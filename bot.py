"""
bot.py — BUCLE PRINCIPAL DE EJECUCIÓN (TESTNET / PAPER TRADING)
==================================================================
Flujo de cada ciclo:
    1. Leer EQUITY (saldo + PnL no realizado). Sin equity fiable NO se opera.
    2. Verificar LÍMITE DE PÉRDIDA DIARIA (Max Daily Drawdown 2%):
       si se supera -> cerrar TODO + alerta + bloqueo permanente del bot.
    3. Leer velas del mercado (fetch_ohlcv).
    4. Pedir una SEÑAL a la estrategia (lógica aislada en strategy.py).
    5. Calcular PLAN DE RIESGO (posición, SL y TP) con RiskManager.
    6. Ejecutar ENTRADA + STOP-LOSS + TAKE-PROFIT como unidad atómica.
       Si falla la protección -> ROLLBACK (cierre inmediato): jamás posiciones sin SL.

Manejo de excepciones:
    * ccxt.NetworkError        -> caída de internet/rate-limit: reintento con backoff.
    * ccxt.ExchangeError       -> error lógico del exchange: se registra y se continúa.
    * ccxt.AuthenticationError -> claves mal: se detiene con alerta (reintentar no ayuda).
    * Cualquier otra excepción -> se registra y el bucle NO se cae.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import re
import sys
import time
import urllib.request
from typing import Any

import ccxt

from config import ConfigError, Settings, load_settings
from exchange_client import create_exchange
from risk_manager import RiskManager, RiskPlan, RiskRejection
from strategy import Signal, atr, create_strategy

log = logging.getLogger("bot")

# Colchón de comisiones para ventas contra el saldo completo en spot: Binance cobra
# ~0.1% de spot fee sobre el activo recibido, así que vender el 100% bruto del saldo
# provoca InsufficientFunds (-2010). Se aplica siempre sobre el balance REAL libre.
SPOT_FEE_BUFFER = 0.001


def setup_logging(settings: Settings) -> None:
    """Logging a consola y, opcionalmente, a archivo (LOG_FILE del .env)."""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if settings.log_file:
        handlers.append(logging.FileHandler(settings.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(name)-10s | %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)  # silenciar ruido HTTP


def log_auth_help(exc: Exception | None = None) -> None:
    """Guía accionable según el CÓDIGO real de error de Binance (-2008/-2015/-2014)."""
    match = re.search(r'"code"\s*:\s*(-?\d+)', str(exc)) if exc else None
    code = match.group(1) if match else None

    if code == "-2008":
        log.critical("DIAGNÓSTICO -2008 'Invalid Api-Key ID': la API key NO EXISTE en Binance.")
        log.critical("  Causas: key borrada o caducada (las de testnet caducan a los 90 días),")
        log.critical("  error al copiarla, o creada en otra plataforma (binance.us, etc.).")
        log.critical("  SOLUCIÓN: crea una key NUEVA en el portal correcto (ver tabla del README).")
    elif code == "-2015":
        log.critical("DIAGNÓSTICO -2015: la key EXISTE pero no tiene permiso/entorno para la acción.")
    elif code == "-2014":
        log.critical("DIAGNÓSTICO -2014 'Invalid HMAC': el API_SECRET no corresponde a esta API_KEY.")
    else:
        log.critical("DIAGNÓSTICO: error de autenticación (código %s).", code if code else "desconocido")

    log.critical("  Cada portal emite claves DISTINTAS e incompatibles entre sí:")
    log.critical("    - Producción   https://www.binance.com/api-management  -> TESTNET=false + ALLOW_LIVE=true")
    log.critical("    - Spot testnet https://testnet.binance.vision          -> TESTNET=true + TESTNET_MODE=sandbox")
    log.critical("    - Demo nuevo   https://demo.binance.com                -> TESTNET=true + TESTNET_MODE=demo")
    log.critical("    - Futuros      https://testnet.binancefuture.com        -> TESTNET=true + EXCHANGE_ID=binanceusdm")
    log.critical("  Otras causas: whitelist de IP que no coincide con tu IP, o falta 'Enable Reading'.")


class TradingBot:
    """Orquesta mercado -> riesgo -> ejecución (siempre con SL/TP obligatorios)."""

    def __init__(
        self,
        settings: Settings,
        exchange: ccxt.Exchange,
        strategy: Any,
        risk: RiskManager,
    ) -> None:
        self.settings = settings
        self.exchange = exchange
        self.strategy = strategy
        self.risk = risk
        self.symbol = settings.symbol
        self._running = True
        self._consecutive_errors = 0
        self._last_entry_ts = 0.0
        self._last_clock_sync = 0.0
        self._active_plan: RiskPlan | None = None  # plan de la posición spot abierta (TP por software)


    # =========================== UTILIDADES DE CUENTA ======================== #
    def _market(self) -> dict:
        return self.exchange.market(self.symbol)

    @staticmethod
    def _is_contract_market(market: dict) -> bool:
        """True para perps/futuros (admiten cortos y reduceOnly); False para spot."""
        return bool(market.get("swap") or market.get("future") or market.get("contract"))

    @staticmethod
    def _position_pnl(position: dict) -> float:
        """PnL no realizado de una posición (soporta naming de distintas versiones)."""
        raw = position.get("unrealizedPnl", position.get("unrealizedPNL", 0.0))
        try:
            return float(raw or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def get_last_price(self) -> float:
        ticker = self.exchange.fetch_ticker(self.symbol)
        price = float(ticker.get("last") or 0.0)
        if price <= 0:
            raise RuntimeError(f"Ticker sin precio válido para {self.symbol}: {ticker}")
        return price

    def get_equity(self) -> float:
        """Equity total en la moneda cotizada (saldo + PnL no realizado).
        Si falla, la excepción sube al bucle y el ciclo se SALTA:
        sin equity fiable no hay gestión de riesgo posible."""
        balance = self.exchange.fetch_balance()
        market = self._market()
        quote = str(market.get("quote", "USDT"))
        quote_total = float(balance.get(quote, {}).get("total") or 0.0)

        if self._is_contract_market(market):
            # Futuros: saldo de la cartera + PnL no realizado de TODAS las posiciones
            unrealized = sum(self._position_pnl(p) for p in self.exchange.fetch_positions())
            return quote_total + unrealized

        # Spot: el activo base comprado también forma parte del patrimonio
        base = str(market.get("base", ""))
        base_total = float(balance.get(base, {}).get("total") or 0.0)
        return quote_total + base_total * self.get_last_price()

    def get_available_quote(self) -> float:
        """Saldo libre en la moneda cotizada (lo máximo que podríamos gastar)."""
        balance = self.exchange.fetch_balance()
        quote = str(self._market().get("quote", "USDT"))
        return float(balance.get(quote, {}).get("free") or 0.0)

    def get_open_position_count(self) -> int:
        """Número de posiciones abiertas en el par (derivados o saldo base en spot)."""
        market = self._market()
        if self._is_contract_market(market):
            positions = self.exchange.fetch_positions([self.symbol])
            return sum(1 for p in positions if float(p.get("contracts") or 0.0) > 0)
        balance = self.exchange.fetch_balance()
        base = str(market.get("base", ""))
        base_total = float(balance.get(base, {}).get("total") or 0.0)
        if base_total <= 0:
            return 0
        min_amount = float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0.0)
        min_notional = max(self._min_notional(market), 5.0)
        try:
            last_price = self.get_last_price()
        except Exception:
            last_price = 0.0
        # Filtrar polvo invendible (< min_notional) en spot
        if last_price > 0 and (base_total * last_price < min_notional):
            return 0
        return 1 if base_total > max(min_amount, 1e-8) else 0

    @staticmethod
    def _min_notional(market: dict) -> float:
        """Notional mínimo (USDT) que exige el mercado para aceptar una orden."""
        limits = market.get("limits") or {}
        value = (limits.get("notional") or {}).get("min") or (limits.get("cost") or {}).get("min") or 0.0
        return float(value or 0.0)

    @staticmethod
    def _amount_step(market: dict) -> float:
        """Incremento mínimo de cantidad (stepSize) que exige el mercado."""
        limits = market.get("limits") or {}
        value = (limits.get("amount") or {}).get("step") or (market.get("precision") or {}).get("amount") or 0.0
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def _sellable_base_amount(self, requested: float) -> float:
        """Balance REAL de BTC disponible para vender, con colchón por comisiones.

        Binance cobra la spot fee sobre el activo recibido: tras comprar `quantity`
        BTC entran ~quantity×(1−0.1%). Vender el 100% bruto provoca InsufficientFunds,
        así que se consulta el balance libre REAL del exchange, se aplica un colchón
        del 0.1% y se trunca hacia abajo al step del mercado."""
        market = self._market()
        balance = self.exchange.fetch_balance()
        base = str(market.get("base", ""))
        free = float(balance.get(base, {}).get("free") or 0.0)
        cap = free * (1.0 - SPOT_FEE_BUFFER)
        sellable = min(requested, cap)
        if sellable <= 0:
            return 0.0
        step = self._amount_step(market)
        if step > 0:
            sellable = math.floor(sellable / step + 1e-9) * step  # truncar SIEMPRE hacia abajo
        # POLVO por debajo del mínimo/step del mercado (p. ej. saldo ya congelado por el
        # Stop-Loss): no hay nada vendible. Se retorna 0.0 ANTES de amount_to_precision,
        # que lanzaría InvalidOrder innecesario y ensuciaría los logs.
        min_amount = float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0.0)
        if sellable <= 0 or sellable < max(step, min_amount):
            return 0.0
        try:
            sellable = float(self.exchange.amount_to_precision(self.symbol, sellable))
        except Exception:
            log.error("No se pudo ajustar la cantidad a la precisión del mercado.", exc_info=True)
            return 0.0
        return min(sellable, cap)  # recorte final por si la precisión redondeó al alza

    def _cooldown_remaining(self) -> float:
        """Segundos que faltan para poder volver a entrar (evita re-entradas tras un SL)."""
        if self.settings.entry_cooldown_seconds <= 0 or self._last_entry_ts <= 0:
            return 0.0
        elapsed = time.time() - self._last_entry_ts
        return max(0.0, self.settings.entry_cooldown_seconds - elapsed)


    def _maybe_resync_clock(self) -> None:
        """Re-sincroniza el reloj local con el del exchange periódicamente (anti -1021).

        `adjustForTimeDifference` solo sincroniza al hacer load_markets(); en sesiones
        largas el reloj del PC puede derivar (p. ej. tras suspensión de Windows) y
        Binance responde -1021 "timestamp outside of the recvWindow". Aquí se
        recalcula el offset cada CLOCK_RESYNC_SECONDS antes de operar.
        """
        now = time.time()
        if now - self._last_clock_sync < self.settings.clock_resync_seconds:
            return
        loader = getattr(self.exchange, "load_time_difference", None)
        if callable(loader):
            try:
                loader()
                self._last_clock_sync = now
                log.info("Reloj re-sincronizado con %s (offset de tiempo aplicado).", self.exchange.id)
            except ccxt.NetworkError:
                log.warning("No se pudo re-sincronizar el reloj; se reintentará en el próximo ciclo.")
            except ccxt.ExchangeError:
                log.warning("El exchange rechazó la sincronización de reloj; se reintentará más tarde.")

    # ========================= ACCIONES DE RIESGO ============================ #
    def send_alert(self, message: str) -> None:
        """Alerta crítica en logs + webhook opcional (Slack/Discord) vía stdlib."""
        log.critical("ALERTA DE RIESGO: %s", message)
        url = self.settings.alert_webhook_url
        if not url:
            return
        try:
            # "text" cubre Slack; "content" cubre Discord (ambos ignoran el otro campo)
            payload = json.dumps({"text": message, "content": message}).encode("utf-8")
            request = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(request, timeout=5):
                pass
            log.info("Alerta enviada al webhook configurado.")
        except Exception:
            log.warning("No se pudo enviar la alerta al webhook (el bot continúa).", exc_info=True)

    def emergency_flat(self) -> bool:
        """Cierra TODAS las posiciones del par y cancela órdenes abiertas.
        Se usa únicamente al superar el Max Daily Drawdown. En spot se cancelan
        primero las órdenes (liberan el saldo congelado) y después se vende el
        balance REAL con colchón de comisiones."""
        ok = True
        market = self._market()
        self._active_plan = None
        try:
            # 1) Cancelar órdenes pendientes (SL/TP): en spot liberan el BTC congelado
            try:
                self.exchange.cancel_all_orders(self.symbol)
                log.warning("Órdenes abiertas canceladas para %s.", self.symbol)
            except Exception as exc:
                log.error("No se pudieron cancelar órdenes abiertas: %s", exc)
            # 2) Cerrar posiciones con el balance realmente disponible
            if self._is_contract_market(market):
                for position in self.exchange.fetch_positions([self.symbol]):
                    contracts = float(position.get("contracts") or 0.0)
                    if contracts <= 0:
                        continue
                    side = str(position.get("side") or "")
                    close_side = "sell" if side == "long" else "buy"
                    try:
                        order = self.exchange.create_order(
                            self.symbol, "market", close_side, contracts, None, {"reduceOnly": True}
                        )
                        log.warning("CIERRE FORZADO %s %s contratos -> orden %s", side, contracts, order.get("id"))
                    except Exception:
                        ok = False
                        log.error("No se pudo cerrar la posición %s", position.get("symbol"), exc_info=True)
            else:
                balance = self.exchange.fetch_balance()
                base = str(market.get("base", ""))
                free = float(balance.get(base, {}).get("free") or 0.0)
                sellable = self._sellable_base_amount(free)
                if sellable > 0:
                    order = self.exchange.create_order(self.symbol, "market", "sell", sellable)
                    log.warning(
                        "CIERRE FORZADO spot: vendidos %.8f %s (de %.8f libres, con colchón %.1f%%) -> orden %s",
                        sellable, base, free, SPOT_FEE_BUFFER * 100.0, order.get("id"),
                    )
                else:
                    log.warning("CIERRE FORZADO spot: sin saldo vendible de %s.", base)
        except Exception:
            ok = False
            log.error("emergency_flat: error durante el cierre forzado", exc_info=True)
        return ok


    def _rollback_position(self, close_side: str, quantity: float, is_contract: bool) -> None:
        """Cierre inmediato si no pudieron colocarse las órdenes de protección.
        En spot: PRIMERO se cancelan las órdenes (liberan el BTC congelado por el
        Stop-Loss) y DESPUÉS se vende el balance real con colchón de comisiones —
        vender primero chocaría con InsufficientFunds por saldo congelado."""
        log.warning("ROLLBACK: se cierra la posición recién abierta (falta de SL/TP).")
        self._active_plan = None
        try:
            try:
                self.exchange.cancel_all_orders(self.symbol)
            except Exception:
                log.warning("ROLLBACK: no se pudieron cancelar órdenes abiertas (se continúa).", exc_info=True)
            if is_contract:
                self.exchange.create_order(self.symbol, "market", close_side, quantity, None, {"reduceOnly": True})
            else:
                sellable = self._sellable_base_amount(quantity)
                if sellable <= 0:
                    raise RuntimeError("balance insuficiente para cerrar en spot (incluso tras cancelar órdenes)")
                self.exchange.create_order(self.symbol, "market", close_side, sellable)
            log.warning("ROLLBACK completado: posición cerrada.")
        except Exception:
            log.critical("ROLLBACK FALLÓ — posición potencialmente desprotegida. Revisa la cuenta.", exc_info=True)
            self.send_alert("ROLLBACK FALLÓ: revisa manualmente las posiciones y órdenes abiertas.")

    def _place_spot_protection(self, plan: RiskPlan, quantity: float) -> None:
        """Protección obligatoria en SPOT (Binance no permite OCO unificado en ccxt):
        1) Stop-Loss en el exchange usando el BALANCE REAL de BTC menos el colchón de
           comisiones (evita InsufficientFunds por vender el ~100% bruto).
        2) Take-Profit como orden si queda saldo libre; si Binance ya congeló el BTC
           dentro de la orden Stop-Loss, el TP lo gestiona el bot por software
           (_manage_software_tp). En ningún caso queda la posición sin SL ni sin TP."""
        sellable = self._sellable_base_amount(quantity)
        if sellable <= 0:
            raise RuntimeError("Balance insuficiente (0 BTC) para colocar el Stop-Loss en spot")
        self._place_protective_order("stop", "sell", sellable, plan.stop_loss_price, False)
        log.info(
            "Stop-Loss spot en %.2f (%.8f BTC = balance real − colchón %.1f%%)",
            plan.stop_loss_price, sellable, SPOT_FEE_BUFFER * 100.0,
        )

        tp_qty = self._sellable_base_amount(quantity)
        if tp_qty > 0:
            try:
                self._place_protective_order("take_profit", "sell", tp_qty, plan.take_profit_price, False)
                log.info("Take-Profit spot colocado en %.2f (%.8f BTC)", plan.take_profit_price, tp_qty)
                return
            except ccxt.ExchangeError:
                log.warning(
                    "No se pudo colocar la orden Take-Profit (probable congelación de saldo por el SL). "
                    "El Take-Profit lo gestionará el bot por software.",
                    exc_info=True,
                )
        else:
            log.warning("Sin saldo libre para la orden Take-Profit; el TP lo gestionará el bot por software.")

    def _manage_software_tp(self, open_positions: int) -> None:
        """Take-Profit gestionado por el bot (respaldo en spot cuando no cupo como orden).

        Si el precio alcanza el TP del plan activo, se cancelan las órdenes abiertas
        (liberan el BTC congelado por el Stop-Loss) y se vende a mercado el balance
        real disponible, con el mismo colchón de comisiones."""
        if self._active_plan is None:
            return
        if open_positions == 0:
            self._active_plan = None  # la posición ya no existe (SL/TP/venta cumplidos)
            return
        plan = self._active_plan
        if plan.side != "long":
            return  # en spot solo existen largos; no hay TP corto que gestionar
        price = self.get_last_price()
        if price < plan.take_profit_price:
            return
        log.info(
            "TAKE-PROFIT por software: precio %.2f alcanza TP %.2f — cerrando posición.",
            price, plan.take_profit_price,
        )
        try:
            self.exchange.cancel_all_orders(self.symbol)
        except Exception:
            log.warning("No se pudieron cancelar órdenes antes del cierre TP (se reintenta en el próximo ciclo).", exc_info=True)
        sellable = self._sellable_base_amount(plan.quantity)
        if sellable <= 0:
            log.critical("TP por software: balance insuficiente para vender; revisa la cuenta.", exc_info=True)
            self.send_alert("TP: balance insuficiente para cerrar la posición. Revisa la cuenta manualmente.")
            return
        try:
            order = self.exchange.create_order(self.symbol, "market", "sell", sellable)
            log.info("TAKE-PROFIT ejecutado a mercado: vendidos %.8f BTC -> orden %s", sellable, order.get("id"))
            self._active_plan = None
            self._last_entry_ts = time.time()  # el cooldown también aplica tras el cierre
        except Exception:
            log.critical("TP por software FALLÓ al vender; se reintentará en el próximo ciclo.", exc_info=True)
            self.send_alert("TP por software falló al cerrar; se reintentará (la posición ya está sin órdenes).")

    def _place_protective_order(
        self, kind: str, close_side: str, quantity: float, price: float, is_contract: bool
    ) -> None:
        """Coloca el SL ('stop') o el TP ('take_profit') calculados por RiskManager."""
        price = float(self.exchange.price_to_precision(self.symbol, price))
        if is_contract:
            # Futuros (estilo Binance USD-M): orden condicionada de mercado + reduceOnly
            order_type = "STOP_MARKET" if kind == "stop" else "TAKE_PROFIT_MARKET"
            self.exchange.create_order(
                self.symbol, order_type, close_side, quantity, None,
                {"stopPrice": price, "reduceOnly": True},
            )
            return
        # Spot (Binance): órdenes condicionadas NATIVAS — la API de spot exige el tipo
        # STOP_LOSS_LIMIT / TAKE_PROFIT_LIMIT con precio límite + stopPrice + timeInForce.
        # (Los métodos unificados create_stop_loss_order cambiaron de firma en ccxt 4.5.x
        #  y binance no los implementa, así que se usa la API nativa directamente.)
        # Regla de Binance para el precio límite:
        #   SELL: el límite del SL va 0,1% POR DEBAJO del disparador (ejecuta en caída)
        #         y el límite del TP 0,1% POR ENCIMA (captura la subida).
        #   BUY : regla espejada (usada solo si hubiera cortos en margen).
        if close_side == "sell":
            offset = 0.999 if kind == "stop" else 1.001
        else:
            offset = 1.001 if kind == "stop" else 0.999
        limit_price = float(self.exchange.price_to_precision(self.symbol, price * offset))
        order_type = "STOP_LOSS_LIMIT" if kind == "stop" else "TAKE_PROFIT_LIMIT"
        self.exchange.create_order(
            self.symbol, order_type, close_side, quantity, limit_price,
            {"stopPrice": price, "timeInForce": "GTC"},
        )

    def open_position(self, plan: RiskPlan) -> bool:
        """Ejecuta ENTRADA + STOP-LOSS + TAKE-PROFIT como unidad atómica.
        Si algo falla tras la entrada, se hace ROLLBACK: jamás queda posición sin SL."""
        market = self._market()
        is_contract = self._is_contract_market(market)

        # --- Ajuste a la precisión del mercado y límites mínimos ---
        try:
            quantity = float(self.exchange.amount_to_precision(self.symbol, plan.quantity))
        except Exception:
            log.error("No se pudo ajustar la cantidad a la precisión del mercado.", exc_info=True)
            return False
        min_amount = float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0.0)
        if quantity < min_amount:
            log.warning(
                "Cantidad %.8f < mínimo del mercado %.8f — no se opera (aumenta capital o reduce STOP_LOSS_PCT).",
                quantity, min_amount,
            )
            return False

        close_side = "sell" if plan.side == "long" else "buy"
        entry_side = "buy" if plan.side == "long" else "sell"  # ccxt exige 'buy'/'sell', nunca 'long'/'short'
        entry_ok = False
        try:
            # 1) ENTRADA a mercado
            entry_order = self.exchange.create_order(self.symbol, "market", entry_side, quantity)
            entry_ok = True
            log.info(
                "ORDEN DE ENTRADA #%s: %s %.8f @ mercado (notional plan: %.2f USDT)",
                entry_order.get("id"), plan.side.upper(), quantity, plan.notional,
            )
            # 2)+3) PROTECCIÓN OBLIGATORIA: STOP-LOSS y TAKE-PROFIT
            if is_contract:
                self._place_protective_order("stop", close_side, quantity, plan.stop_loss_price, True)
                log.info("Stop-Loss colocado en %.2f", plan.stop_loss_price)
                self._place_protective_order("take_profit", close_side, quantity, plan.take_profit_price, True)
                log.info("Take-Profit colocado en %.2f", plan.take_profit_price)
            else:
                # Spot: balance REAL con colchón de comisiones + TP como orden o por software
                self._place_spot_protection(plan, quantity)
        except Exception as exc:
            log.error("Fallo al ejecutar entrada/protección: %s", exc, exc_info=True)
            if entry_ok:
                self._rollback_position(close_side, quantity, is_contract)
            return False

        self._last_entry_ts = time.time()
        if not is_contract:
            self._active_plan = plan  # respaldo de TP por software (spot sin OCO)
        return True


    # ============================ BUCLE PRINCIPAL ============================ #
    def run_cycle(self) -> None:
        """Un ciclo completo: mercado -> riesgo -> ejecución.
        Si algo falla, la excepción sube a run() y se reintenta en la siguiente vuelta."""

        # --- PASO 0: re-sincronía del reloj (previene -1021 en sesiones largas) ---
        self._maybe_resync_clock()

        # --- PASO 1: EQUITY (base de todo el cálculo de riesgo) ---
        equity = self.get_equity()

        # --- PASO 2: límite de pérdida diaria (MAX DAILY DRAWDOWN) ---
        self.risk.update_daily_baseline(equity, dt.date.today())
        if self.risk.halted:
            return  # ya se cerró y alertó: modo solo-monitoreo hasta reinicio manual
        if equity <= 0:
            # Sin fondos no hay riesgo que calcular: se monitorea sin operar
            # (evita falsos bloqueos y divisiones por cero con equity 0).
            log.warning(
                "Equity %.2f: cuenta sin fondos — se omite el ciclo. Recarga al menos ~10 USDT para operar BTC/USDT.",
                equity,
            )
            return
        if not self.risk.check_daily_drawdown(equity):
            # Se alcanzó el límite diario: cerrar TODO + alerta + bloqueo permanente
            closed_ok = self.emergency_flat()
            self.send_alert(
                f"BLOQUEO DIARIO ACTIVADO ({self.exchange.id} {self.symbol}) — {self.risk.halt_reason}. "
                f"Cierre de posiciones: {'OK' if closed_ok else 'FALLÓ (revisar manualmente)'}."
            )
            return

        # --- PASO 3: lectura de mercado ---
        limit_candles = max(self.settings.sma_slow, self.settings.macd_slow + self.settings.macd_signal, self.settings.rsi_period, self.settings.atr_period) + 10
        candles = self.exchange.fetch_ohlcv(
            self.symbol, self.settings.timeframe, limit=limit_candles
        )

        # --- PASO 4: decisión de estrategia (lógica aislada y reemplazable) ---
        decision = self.strategy.generate_signal(candles)

        # --- PASO 5: estado de posiciones + resumen del ciclo ---
        open_positions = self.get_open_position_count()
        # TP gestionado por software (respaldo cuando no cupo como orden en spot)
        self._manage_software_tp(open_positions)
        pnl_today = self.risk.daily_pnl(equity)
        log.info(
            "equity=%.2f | pnl_día=%s | %s | posiciones_abiertas=%d",
            equity,
            f"{pnl_today:+.2f} USDT" if pnl_today is not None else "n/a",
            decision.reason,
            open_positions,
        )
        if decision.signal is Signal.HOLD:
            return

        allowed, reason = self.risk.can_open_position(open_positions)
        if not allowed:
            log.info("Sin entrada: %s", reason)
            return
        remaining = self._cooldown_remaining()
        if remaining > 0:
            log.info("Sin entrada: cooldown activo (%.0f s restantes).", remaining)
            return

        market = self._market()
        side = "long" if decision.signal is Signal.OPEN_LONG else "short"
        if side == "short" and not self._is_contract_market(market):
            log.info("Sin entrada: el mercado spot no permite posiciones cortas.")
            return

        # --- PASO 6: PLAN DE RIESGO (obligatorio antes de cualquier orden) ---
        entry_price = float(candles[-1][4])  # cierre de la última vela
        atr_val = atr(candles, self.settings.atr_period) if self.settings.use_atr_sl_tp else None

        try:
            plan = self.risk.calculate_position_size(
                side=side,
                entry_price=entry_price,
                equity=equity,
                available_quote=self.get_available_quote(),
                min_notional=self._min_notional(market),
                amount_step=self._amount_step(market),
                atr_value=atr_val,
            )
        except RiskRejection as exc:
            log.info("Operación RECHAZADA por riesgo: %s", exc)
            return

        log.info(
            "PLAN DE RIESGO: %s %.6f @ %.2f | SL %.2f | TP %.2f | "
            "riesgo máx %.2f USDT (%.2f%% del capital)",
            plan.side.upper(), plan.quantity, plan.entry_price,
            plan.stop_loss_price, plan.take_profit_price,
            plan.risk_amount, plan.risk_pct_of_equity,
        )

        # --- PASO 7: ejecución (entrada + SL + TP atómicos) ---
        if self.open_position(plan):
            log.info("Entrada ejecutada y protegida con Stop-Loss y Take-Profit.")


    def _bootstrap(self) -> None:
        """Preparación inicial: mercados, validación del par y equity de referencia."""
        # Carga de mercados con reintentos (puede fallar por red)
        for attempt in range(1, 6):
            try:
                self.exchange.load_markets()
                break
            except ccxt.NetworkError:
                wait = min(30, 2 ** attempt)
                log.warning("No se pudieron cargar los mercados (intento %d/5). Reintento en %ds...", attempt, wait)
                time.sleep(wait)
        else:
            raise RuntimeError("No se pudieron cargar los mercados tras 5 intentos (problema de red).")

        if self.symbol not in self.exchange.markets:
            raise ConfigError(f"El símbolo '{self.symbol}' no existe en {self.exchange.id}. Revisa SYMBOL en .env.")

        market = self._market()
        log.info(
            "Mercados cargados: %d | par %s (%s)",
            len(self.exchange.markets), self.symbol,
            "derivado" if self._is_contract_market(market) else "spot",
        )

        # Equity inicial = referencia del límite de pérdida diaria
        equity = self.get_equity()
        self.risk.update_daily_baseline(equity, dt.date.today())
        log.info(
            "Equity inicial: %.2f USDT | límite de pérdida diaria: %.2f USDT (%.2f%%)",
            equity, equity * self.settings.max_daily_drawdown_pct / 100.0,
            self.settings.max_daily_drawdown_pct,
        )

    def run(self) -> None:
        """Bucle infinito con manejo exhaustivo de excepciones: NUNCA debe crashear."""
        log.info("=" * 72)
        log.info(
            "Bot iniciado | ccxt %s | modo=%s | exchange=%s | par=%s | tf=%s | estrategia=%s",
            ccxt.__version__,
            "TESTNET/DEMO (paper trading)" if self.settings.testnet else "¡¡MERCADO REAL!!",
            self.exchange.id, self.symbol, self.settings.timeframe, self.settings.strategy_name,
        )
        log.info(
            "Riesgo: %.2f%% por operación | SL %s | TP %s | drawdown diario %.2f%% | máximo %d posición(es)",
            self.settings.risk_per_trade_pct,
            f"ATR x {self.settings.atr_sl_mult}" if self.settings.use_atr_sl_tp else f"{self.settings.stop_loss_pct:.2f}%",
            f"ATR x {self.settings.atr_tp_mult}" if self.settings.use_atr_sl_tp else f"{self.settings.take_profit_pct:.2f}%",
            self.settings.max_daily_drawdown_pct,
            self.settings.max_concurrent_positions,
        )
        self._bootstrap()

        while self._running:
            try:
                self.run_cycle()
                self._consecutive_errors = 0
                self._sleep(self.settings.poll_interval_seconds)
            except ccxt.NetworkError as exc:
                # Caída de internet / timeout / rate-limit: reintento con backoff, sin crashear
                self._consecutive_errors += 1
                delay = min(60, 5 * self._consecutive_errors)
                log.warning("NetworkError (temporal): %s — reintento en %ds (fallos seguidos: %d)",
                            exc, delay, self._consecutive_errors)
                self._sleep(delay)
            except ccxt.AuthenticationError as exc:
                # Claves inválidas: reintentar no ayuda -> se detiene con alerta
                log.critical("AuthenticationError: revisa API_KEY/API_SECRET. Deteniendo el bot.", exc_info=True)
                log_auth_help(exc)
                self.send_alert(f"Autenticación fallida en {self.exchange.id}: revisa las claves (error {exc}).")
                self._running = False
            except ccxt.ExchangeError as exc:
                # Error lógico del exchange (orden rechazada, par inválido...): se registra y se continúa
                self._consecutive_errors += 1
                log.error("ExchangeError: %s — continúo con el siguiente ciclo.", exc, exc_info=True)
                self._sleep(self.settings.poll_interval_seconds)
            except KeyboardInterrupt:
                log.info("Interrupción manual recibida (Ctrl+C): cerrando de forma ordenada...")
                self._running = False
            except Exception:
                # Último recurso: cualquier bug o imprevisto no debe tumbar el bucle
                self._consecutive_errors += 1
                log.exception("Error inesperado (continuaré en el próximo ciclo).")
                self._sleep(self.settings.poll_interval_seconds)

        log.info("Bot detenido de forma ordenada. Las órdenes de protección del testnet permanecen activas.")


    @staticmethod
    def _sleep(seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


# ================================== ENTRY POINT ============================= #
def main() -> int:
    """Punto de entrada: configuración -> exchange -> riesgo -> bucle."""
    # El logging se configura lo antes posible; la config puede fallar antes de eso
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"[ERROR DE CONFIGURACIÓN] {exc}", file=sys.stderr)
        return 1

    setup_logging(settings)
    try:
        exchange = create_exchange(settings)
        strategy = create_strategy(settings)
        risk = RiskManager(
            risk_per_trade_pct=settings.risk_per_trade_pct,
            max_daily_drawdown_pct=settings.max_daily_drawdown_pct,
            stop_loss_pct=settings.stop_loss_pct,
            take_profit_pct=settings.take_profit_pct,
            max_notional_pct_of_equity=settings.max_notional_pct_of_equity,
            max_concurrent_positions=settings.max_concurrent_positions,
            use_atr_sl_tp=settings.use_atr_sl_tp,
            atr_sl_mult=settings.atr_sl_mult,
            atr_tp_mult=settings.atr_tp_mult,
        )
        bot = TradingBot(settings, exchange, strategy, risk)
        bot.run()
        return 0
    except ConfigError as exc:
        log.error("Configuración inválida: %s", exc)
        return 1
    except ccxt.BaseError as exc:
        # Agrupa NetworkError y ExchangeError fatales de arranque (p.ej. load_markets)
        log.critical("Error fatal de ccxt durante el arranque: %s", exc, exc_info=True)
        if isinstance(exc, ccxt.AuthenticationError):
            log_auth_help(exc)
        return 1
    except KeyboardInterrupt:
        log.info("Saliendo...")
        return 0


if __name__ == "__main__":
    sys.exit(main())
