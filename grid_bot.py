"""
grid_bot.py — Bot de GRID TRADING (rejilla) para Binance Spot — Altcoins Top
==============================================================================
Estrategia: rejilla de órdenes límite de compra por DEBAJO del precio actual;
cuando una celda compra, se coloca su Take-Profit (venta) una celda POR ENCIMA.
Con spacing 0.8%–1.2% y comisiones Spot (0.1%+0.1%), cada ciclo deja un margen
neto de ~0.6%–1.0% del notional de la celda.

DISEÑO (requisitos estrictos):
  * Lista blanca: SOL/USDT, LINK/USDT, ADA/USDT, AVAX/USDT.
  * Capital: 100 USDT -> 60% USDT libre (reserva/recompras) + 40% en la grilla.
  * 8–12 celdas solicitadas; el bot AUTOCAPA al máximo factible porque cada
    orden debe ser >= 5 USDT (mínimo de Binance) y el presupuesto (40 USDT)
    manda. Con steps reales la orden mínima suele ser ~5.1–6 USDT.
  * Toda orden pasa por amount_to_precision/price_to_precision y umbrales
    max(step, min_amount) y notional >= 5 USDT ANTES de enviarse.
  * Freno GLOBAL: si el capital cae por debajo de 95 USDT (-5%), se cancela
    TODO, se vende la altcoin a mercado y el bot queda en PAUSA.
  * Usa las credenciales del .env (misma configuración que bot.py).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import os
import sys
import time
from dataclasses import dataclass

import ccxt

from bot import log_auth_help, setup_logging
from config import ConfigError, Settings, load_settings
from exchange_client import create_exchange

log = logging.getLogger("grid")

# ------------------------------ Parámetros fijos ----------------------------- #
WHITELIST: tuple[str, ...] = ("SOL/USDT", "LINK/USDT", "ADA/USDT", "AVAX/USDT")
SPOT_FEE_BUFFER = 0.001              # 0.1%: comisión estándar de Binance Spot
CAPITAL_USDT = 100.0                 # capital total asignado al bot
FREE_ALLOCATION_PCT = 0.60           # 60% en USDT libre (reserva / recompras)
GRID_ALLOCATION_PCT = 0.40           # 40% desplegado en órdenes de la grilla
GRID_LEVELS_MIN, GRID_LEVELS_MAX = 8, 12        # celdas solicitadas
GRID_SPACING_MIN, GRID_SPACING_MAX = 0.008, 0.012   # 0.8%–1.2% por celda
BINANCE_MIN_NOTIONAL = 5.0           # mínimo de Binance por orden (USDT)
GLOBAL_STOP_USDT = 95.0              # freno global: capital < 95 => cierre + pausa
ORDER_BUDGET_TOLERANCE = 1.05        # tolerancia del 5% si el step obliga a subir la orden


@dataclass(frozen=True)
class GridConfig:
    """Configuración validada de la rejilla (sobre un único par de la lista blanca)."""
    symbol: str
    levels: int                      # celdas solicitadas (se autocapan al ejecutar)
    spacing: float                   # ancho de celda en fracción (0.008–0.012)
    capital: float                   # capital total del bot (USDT)
    grid_budget: float               # capital asignado a las órdenes (40%)
    free_reserve: float              # capital que debe quedar libre (60%)

    @property
    def order_usdt_target(self) -> float:
        """USDT por celda del presupuesto teórico (se ajusta al autocap real)."""
        return self.grid_budget / self.levels


def build_grid_config() -> GridConfig:
    """Lee y valida la configuración de la grilla (.env opcional) con valores fijos seguros."""
    symbol = os.getenv("GRID_SYMBOL", WHITELIST[0]).strip().upper()
    if symbol not in WHITELIST:
        raise ConfigError(f"GRID_SYMBOL='{symbol}' no está en la lista blanca: {', '.join(WHITELIST)}")

    raw_levels = os.getenv("GRID_LEVELS", str(GRID_LEVELS_MIN)).strip()
    try:
        requested_levels = int(float(raw_levels))
    except ValueError as exc:
        raise ConfigError(f"GRID_LEVELS debe ser un entero (8-12), recibido: {raw_levels!r}") from exc
    if not (GRID_LEVELS_MIN <= requested_levels <= GRID_LEVELS_MAX):
        raise ConfigError(
            f"GRID_LEVELS debe estar entre {GRID_LEVELS_MIN} y {GRID_LEVELS_MAX} (recibido: {requested_levels})"
        )

    raw_spacing = os.getenv("GRID_SPACING_PCT", "1.0").strip()
    try:
        spacing = float(raw_spacing) / 100.0
    except ValueError as exc:
        raise ConfigError(f"GRID_SPACING_PCT debe ser numérico, recibido: {raw_spacing!r}") from exc
    if not (GRID_SPACING_MIN - 1e-12 <= spacing <= GRID_SPACING_MAX + 1e-12):
        raise ConfigError(f"GRID_SPACING_PCT debe estar entre 0.8 y 1.2 (porcentaje), recibido: {raw_spacing}")

    grid_budget = CAPITAL_USDT * GRID_ALLOCATION_PCT      # 40 USDT para las órdenes
    return GridConfig(
        symbol=symbol,
        levels=requested_levels,
        spacing=spacing,
        capital=CAPITAL_USDT,
        grid_budget=grid_budget,
        free_reserve=CAPITAL_USDT * FREE_ALLOCATION_PCT,
    )


class GridStrategy:
    """Lógica PURA de la rejilla (sin red ni exchange): niveles y objetivos.

      * Compras límite por DEBAJO del precio: precio_i = actual × (1 − spacing×i)
      * Tras cada compra, Take-Profit (venta) una celda POR ENCIMA: compra × (1 + spacing)
      * Tras vender, se rearma la compra un nivel por debajo del TP (recompra)
    Margen bruto por ciclo = spacing; neto = spacing − 2×0.1% (comisiones).
    """

    def __init__(self, config: GridConfig) -> None:
        self.config = config

    def buy_prices(self, current_price: float, levels: int) -> list[float]:
        """Precios de las celdas de compra, del más cercano al más lejano."""
        if current_price <= 0:
            raise ValueError(f"precio actual inválido: {current_price}")
        return [current_price * (1.0 - self.config.spacing * i) for i in range(1, levels + 1)]

    def sell_target_price(self, buy_price: float) -> float:
        """Take-Profit de la celda: una celda por encima del precio de compra."""
        return buy_price * (1.0 + self.config.spacing)

    def rearm_buy_price(self, sell_price: float) -> float:
        """Recompra rearmada un nivel por debajo del precio de venta ejecutado."""
        return sell_price * (1.0 - self.config.spacing)


class GridBot:
    """Gestiona una rejilla sobre un par de la lista blanca con riesgo estricto."""

    def __init__(self, settings: Settings, exchange: ccxt.Exchange, config: GridConfig) -> None:
        self.settings = settings
        self.exchange = exchange
        self.config = config
        self.symbol = config.symbol
        self.strategy = GridStrategy(config)
        self._running = True
        self._halted = False
        self._last_clock_sync = 0.0
        self._cells_filled = 0                        # celdas ejecutadas en esta sesión
        self._baseline_date: dt.date | None = None
        self._baseline_equity: float | None = None
        # Órdenes vivas que sigue el bot: {id: {"side", "price", "quantity", "usdt"}}
        self._buy_orders: dict[str, dict] = {}
        self._sell_orders: dict[str, dict] = {}
        self._usdt_per_cell: float = 0.0   # USDT por celda real (tras autocap de niveles)

    # ------------------------------ Utilidades ------------------------------ #
    def _market(self) -> dict:
        return self.exchange.market(self.symbol)

    @staticmethod
    def _amount_step(market: dict) -> float:
        limits = market.get("limits") or {}
        value = (limits.get("amount") or {}).get("step") or (market.get("precision") or {}).get("amount") or 0.0
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _min_amount(market: dict) -> float:
        limits = market.get("limits") or {}
        try:
            return float((limits.get("amount") or {}).get("min") or 0.0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _min_notional(market: dict) -> float:
        limits = market.get("limits") or {}
        value = (limits.get("notional") or {}).get("min") or (limits.get("cost") or {}).get("min") or 0.0
        try:
            return float(value or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def get_price(self) -> float:
        ticker = self.exchange.fetch_ticker(self.symbol)
        price = float(ticker.get("last") or 0.0)
        if price <= 0:
            raise RuntimeError(f"Ticker sin precio válido para {self.symbol}: {ticker}")
        return price

    def get_balances(self) -> tuple[float, float]:
        """(USDT total libre+congelado, cantidad total de la altcoin). Cuenta dedicada al bot."""
        balance = self.exchange.fetch_balance()
        market = self._market()
        quote = str(market.get("quote", "USDT"))
        base = str(market.get("base", ""))
        quote_total = float(balance.get(quote, {}).get("total") or 0.0)
        base_total = float(balance.get(base, {}).get("total") or 0.0)
        return quote_total, base_total

    def get_equity(self) -> float:
        """Capital total del bot: USDT (libre + en órdenes) + valor actual de la altcoin."""
        quote_total, base_total = self.get_balances()
        return quote_total + base_total * self.get_price()

    def _sellable_base_amount(self, requested: float) -> float:
        """Balance REAL de altcoin disponible para vender, con colchón de comisiones.

        Vender el 100% bruto provoca InsufficientFunds: se usa el balance libre
        REAL, colchón del 0.1% y truncado al step. El polvo por debajo de
        max(step, min_amount) retorna 0.0 ANTES de amount_to_precision (anti InvalidOrder)."""
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
            sellable = math.floor(sellable / step + 1e-9) * step
        if sellable <= 0 or sellable < max(step, self._min_amount(market)):
            return 0.0
        try:
            sellable = float(self.exchange.amount_to_precision(self.symbol, sellable))
        except Exception:
            log.error("No se pudo ajustar la cantidad a la precisión del mercado.", exc_info=True)
            return 0.0
        return min(sellable, cap)


    def _maybe_resync_clock(self) -> None:
        """Re-sincroniza el reloj local con el exchange (anti -1021) cada 30 minutos."""
        now = time.time()
        if now - self._last_clock_sync < 1800:
            return
        loader = getattr(self.exchange, "load_time_difference", None)
        if callable(loader):
            try:
                loader()
                self._last_clock_sync = now
                log.info("Reloj re-sincronizado con %s.", self.exchange.id)
            except ccxt.NetworkError:
                log.warning("No se pudo re-sincronizar el reloj; se reintentará.")

    # ----------------------- Dimensionado de órdenes ------------------------ #
    def _min_feasible_notional(self, price: float, levels: int) -> float:
        """Menor notional de entrada que garantiza que el TAKE-PROFIT de la celda MÁS
        PROFUNDA cumpla también el mínimo de 5 USDT tras comisión (0.1%) y truncado al step."""
        market = self._market()
        step = self._amount_step(market)
        if step <= 0 or price <= 0:
            return float("inf")
        min_notional = max(self._min_notional(market), BINANCE_MIN_NOTIONAL)
        worst_buy = price * (1.0 - self.config.spacing * max(levels, 1))
        worst_tp = worst_buy * (1.0 + self.config.spacing)
        if worst_tp <= 0:
            return float("inf")
        # El TP vende lo RECIBIDO (entrada − comisión − truncado): primero se calcula el
        # recibido mínimo (step-múltiplo) y a partir de él el ENVIADO mínimo también step-múltiplo.
        min_recv = math.ceil((min_notional / worst_tp) / step - 1e-9) * step
        min_sent = math.ceil((min_recv / (1.0 - SPOT_FEE_BUFFER)) / step - 1e-9) * step
        min_sent = max(min_sent, step, self._min_amount(market))
        return min_sent * price

    def _order_quantity(self, usdt_amount: float, price: float) -> float | None:
        """Cantidad para una orden de `usdt_amount` USDT, con precisión y umbrales.
        Retorna None (sin enviar nada) si la orden sería inválida para Binance."""
        market = self._market()
        step = self._amount_step(market)
        if step <= 0 or price <= 0:
            return None
        min_notional = max(self._min_notional(market), BINANCE_MIN_NOTIONAL)
        floor_qty = math.floor((usdt_amount / price) / step + 1e-9) * step
        needed = max(step, self._min_amount(market), math.ceil((min_notional / price) / step - 1e-9) * step)
        qty = max(floor_qty, needed)
        if qty * price > usdt_amount * ORDER_BUDGET_TOLERANCE:
            log.warning(
                "Orden de %.2f USDT quedaría en %.2f USDT por el step del mercado (%.8f); se omite para respetar el presupuesto.",
                usdt_amount, qty * price, step,
            )
            return None
        try:
            qty = float(self.exchange.amount_to_precision(self.symbol, qty))
        except Exception:
            log.error("No se pudo ajustar la cantidad a la precisión del mercado.", exc_info=True)
            return None
        # Verificación final de umbrales ANTES de enviar (anti InvalidOrder/InsufficientFunds)
        if qty <= 0 or qty < max(step, self._min_amount(market)) or qty * price + 1e-9 < min_notional:
            log.warning("Orden descartada por umbrales del mercado (cantidad %.8f, notional %.2f USDT).", qty, qty * price)
            return None
        return qty

    def _place_limit(self, side: str, price: float, usdt_amount: float | None = None,
                     quantity: float | None = None) -> dict | None:
        """Envía una orden límite ya validada (precisión + umbrales) y la registra para seguimiento."""
        px = float(self.exchange.price_to_precision(self.symbol, price))
        if quantity is None:
            if usdt_amount is None:
                return None
            quantity = self._order_quantity(usdt_amount, px)
            if quantity is None:
                return None
        try:
            order = self.exchange.create_order(self.symbol, "limit", side, quantity, px)
        except (ccxt.InvalidOrder, ccxt.InsufficientFunds) as exc:
            log.warning("Orden %s rechazada por el exchange (%.8f @ %.4f): %s", side.upper(), quantity, px, exc)
            return None
        except ccxt.ExchangeError as exc:
            log.error("Error del exchange al colocar %s (%.8f @ %.4f): %s", side.upper(), quantity, px, exc)
            return None
        tracked = {"side": side, "price": px, "quantity": float(quantity), "usdt": float(quantity) * px}
        (self._buy_orders if side == "buy" else self._sell_orders)[str(order["id"])] = tracked
        log.info("ORDEN %s colocada: %.8f @ %.4f (≈%.2f USDT) | id=%s",
                 side.upper(), quantity, px, float(quantity) * px, order.get("id"))
        return order


    # ------------------------- Ciclo de vida de la grilla -------------------- #
    def _effective_levels(self, price: float) -> int:
        """Celdas realmente desplegables: respeta presupuesto (40 USDT), mínimo de 5 USDT
        por orden Y viabilidad del Take-Profit de la celda más profunda. Converge en pocas
        iteraciones con una secuencia monótona decreciente."""
        levels = self.config.levels
        min_order = float("inf")
        for _ in range(4):
            min_order = self._min_feasible_notional(price, levels)
            if min_order <= 0 or min_order == float("inf"):
                return 0
            affordable = int(self.config.grid_budget // min_order)
            new_levels = max(0, min(levels, affordable))
            if new_levels >= levels:
                break
            levels = new_levels
        if levels < self.config.levels:
            log.warning(
                "Autocap de celdas: %d solicitadas -> %d factibles (celda mínima viable para este "
                "par/precio = %.2f USDT, TP de la celda más profunda incluido; presupuesto %.2f USDT).",
                self.config.levels, levels, min_order, self.config.grid_budget,
            )
        return levels

    def _place_initial_grid(self) -> None:
        """Coloca la grilla inicial de compras solo si no hay posición ni órdenes vivas."""
        _, base_total = self.get_balances()
        min_amount = self._min_amount(self._market())
        if base_total > max(min_amount, 1e-8):
            log.info("Ya hay saldo de altcoin (%.8f): no se coloca grilla inicial.", base_total)
            return
        price = self.get_price()
        levels = self._effective_levels(price)
        if levels < 1:
            log.error("No hay celdas desplegables con el presupuesto actual; se omite la grilla inicial.")
            return
        usdt_per_cell = self.config.grid_budget / levels
        self._usdt_per_cell = usdt_per_cell
        prices = self.strategy.buy_prices(price, levels)
        log.info(
            "GRILLA INICIAL: %d celdas | precio %.4f | spacing %.2f%% | %.2f USDT por celda (neto esperado %.2f%%/ciclo)",
            levels, price, self.config.spacing * 100.0, usdt_per_cell,
            (self.config.spacing - 2 * SPOT_FEE_BUFFER) * 100.0,
        )
        placed = 0
        for p in prices:
            if self._place_limit("buy", p, usdt_amount=usdt_per_cell):
                placed += 1
        log.info("Grilla: %d/%d órdenes de compra colocadas (presupuesto %.2f USDT, libre %.2f USDT).",
                 placed, len(prices), self.config.grid_budget, self.config.free_reserve)

    def _place_take_profit(self, quantity: float, buy_price: float) -> None:
        """TP de la celda: venta límite una celda por encima, con balance REAL y colchón."""
        target = self.strategy.sell_target_price(buy_price)
        # Lo que ESTA celda recibió tras la comisión: no vender más de lo que ingresó
        expected_received = quantity * (1.0 - SPOT_FEE_BUFFER)
        sellable = self._sellable_base_amount(expected_received)
        min_notional = max(self._min_notional(self._market()), BINANCE_MIN_NOTIONAL)
        if sellable <= 0 or sellable * target + 1e-9 < min_notional:
            log.warning(
                "TAKE-PROFIT omitido: saldo vendible %.8f (x %.4f = %.2f USDT) insuficiente para el mínimo.",
                sellable, target, sellable * target,
            )
            return
        if self._place_limit("sell", target, quantity=sellable):
            log.info("TAKE-PROFIT colocado: VENTA %.8f @ %.4f (celda +%.2f%%)",
                     sellable, target, self.config.spacing * 100.0)

    def _sync_orders(self) -> None:
        """Detecta celdas ejecutadas y gestiona TP/recompras (comuta el ciclo de la grilla).

        Se itera sobre FOTOS del estado al inicio del ciclo: las órdenes que este mismo
        pase coloque (TP o recompra) no deben procesarse como 'desaparecidas'."""
        try:
            open_ids = {str(o["id"]) for o in self.exchange.fetch_open_orders(self.symbol)}
        except (ccxt.NetworkError, ccxt.ExchangeError) as exc:
            log.warning("No se pudieron consultar las órdenes abiertas (%s); se reintentará.", exc)
            return

        buy_snapshot = list(self._buy_orders.items())
        sell_snapshot = list(self._sell_orders.items())

        # --- Compras ejecutadas -> colocar Take-Profit de la celda ---
        for oid, info in buy_snapshot:
            if self._buy_orders.get(oid) is None:      # ya procesada en este pase
                continue
            if oid in open_ids:
                continue
            try:
                order = self.exchange.fetch_order(oid, self.symbol)
            except ccxt.NetworkError as exc:
                log.warning("Error de red al leer la compra %s (%s); se reintentará.", oid, exc)
                continue
            except ccxt.ExchangeError:
                log.warning("No se pudo leer la compra %s; se retira del seguimiento.", oid)
                del self._buy_orders[oid]
                continue
            filled = float(order.get("filled") or 0.0)
            if str(order.get("status")) == "closed" and filled > 0:
                self._cells_filled += 1
                log.info("CELDA %d EJECUTADA: COMPRA %.8f @ %.4f (≈%.2f USDT)",
                         self._cells_filled, filled, info["price"], filled * info["price"])
                self._place_take_profit(filled, info["price"])
            else:
                log.info("Compra %s sin ejecutar (status=%s); se retira del seguimiento.", oid, order.get("status"))
            del self._buy_orders[oid]

        # --- Ventas ejecutadas: ciclo completo -> rearmar recompra ---
        for oid, info in sell_snapshot:
            if self._sell_orders.get(oid) is None:
                continue
            if oid in open_ids:
                continue
            try:
                order = self.exchange.fetch_order(oid, self.symbol)
            except ccxt.NetworkError as exc:
                log.warning("Error de red al leer la venta %s (%s); se reintentará.", oid, exc)
                continue
            except ccxt.ExchangeError:
                log.warning("No se pudo leer la venta %s; se retira del seguimiento.", oid)
                del self._sell_orders[oid]
                continue
            filled = float(order.get("filled") or 0.0)
            if str(order.get("status")) == "closed" and filled > 0:
                log.info("CICLO COMPLETO: VENTA %.8f @ %.4f -> ≈%.2f USDT (bruto) | rearmo recompra",
                         filled, info["price"], filled * info["price"])
                rearm = self.strategy.rearm_buy_price(info["price"])
                # La recompra usa el tamaño real de celda (los ~5 USDT del TP no llegan al mínimo)
                self._place_limit("buy", rearm, usdt_amount=self._usdt_per_cell or self.config.order_usdt_target)
            else:
                log.info("Venta %s sin ejecutar (status=%s); se retira del seguimiento.", oid, order.get("status"))
            del self._sell_orders[oid]


    # --------------------------- Riesgo y frenos ----------------------------- #
    def _update_daily_baseline(self, equity: float) -> None:
        """Referencia de PnL diario (solo se ancla con equity positivo)."""
        today = dt.date.today()
        if self._baseline_date != today:
            self._baseline_date = today
            self._baseline_equity = None
        if self._baseline_equity is None and equity > 0:
            self._baseline_equity = equity

    def _daily_pnl(self, equity: float) -> float | None:
        if self._baseline_equity is None:
            return None
        return equity - self._baseline_equity

    def _emergency_stop(self, equity: float) -> None:
        """FRENO GLOBAL: capital < 95 USDT -> cancelar TODO, vender altcoin y pausar."""
        self._halted = True
        loss = CAPITAL_USDT - equity
        log.critical("=" * 72)
        log.critical("FRENO GLOBAL ACTIVADO: capital %.2f USDT < umbral %.2f USDT (pérdida %.2f USDT / %.2f%%).",
                     equity, GLOBAL_STOP_USDT, loss, loss / CAPITAL_USDT * 100.0)
        # a) Cancelar TODAS las órdenes límite de la grilla
        try:
            self.exchange.cancel_all_orders(self.symbol)
            log.critical("1) Órdenes de la grilla canceladas para %s.", self.symbol)
        except Exception:
            log.critical("1) FALLÓ cancelar órdenes: revisa la cuenta manualmente.", exc_info=True)
        self._buy_orders.clear()
        self._sell_orders.clear()
        # b) Venta a mercado de TODA la altcoin (balance real - colchón de comisiones)
        try:
            market = self._market()
            base = str(market.get("base", ""))
            _, base_total = self.get_balances()
            sellable = self._sellable_base_amount(base_total)
            min_notional = max(self._min_notional(market), BINANCE_MIN_NOTIONAL)
            if sellable > 0 and sellable * self.get_price() + 1e-9 >= min_notional:
                order = self.exchange.create_order(self.symbol, "market", "sell", sellable)
                log.critical("2) VENTA A MERCADO ejecutada: %.8f %s -> orden %s", sellable, base, order.get("id"))
            else:
                log.critical("2) Sin saldo vendible de %s para la venta de emergencia (polvo/mínimos).", base)
        except Exception:
            log.critical("2) FALLÓ la venta de emergencia: revisa la cuenta manualmente.", exc_info=True)
        # c) Pausa del bot
        log.critical("3) Bot EN PAUSA: no operará hasta un reinicio manual (decisión humana).")
        log.critical("=" * 72)

    def _log_status(self, equity: float) -> None:
        """Monitoreo detallado: equity, PnL diario, saldo y celdas activas/ejecutadas."""
        quote_total, base_total = self.get_balances()
        price = self.get_price()
        pnl = self._daily_pnl(equity)
        log.info(
            "EQUITY %.2f USDT | pnl_día=%s | USDT %.2f | %s %.8f (≈%.2f USDT) | compras=%d | TPs=%d | celdas_ejecutadas=%d",
            equity,
            f"{pnl:+.2f} USDT" if pnl is not None else "n/a",
            quote_total,
            str(self._market().get("base", "")), base_total, base_total * price,
            len(self._buy_orders), len(self._sell_orders), self._cells_filled,
        )

    # ------------------------------ Bucle principal -------------------------- #
    def run_cycle(self) -> None:
        """Un ciclo: freno global -> detección de celdas -> repostar grilla -> monitoreo."""
        self._maybe_resync_clock()

        # PASO 1: FRENO GLOBAL (siempre primero)
        equity = self.get_equity()
        if equity < GLOBAL_STOP_USDT and not self._halted:
            self._emergency_stop(equity)
            return
        if self._halted:
            return

        # PASO 2: PnL diario y detección de compras/ventas ejecutadas
        self._update_daily_baseline(equity)
        self._sync_orders()

        # PASO 3: si la grilla quedó vacía y no hay posición, colocar una nueva
        if not self._buy_orders and not self._sell_orders:
            self._place_initial_grid()

        # PASO 4: monitoreo
        self._log_status(equity)


    def _bootstrap(self) -> None:
        """Arranque: mercados + rejilla limpia (cancela órdenes previas para evitar duplicados)."""
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
            raise ConfigError(f"El símbolo '{self.symbol}' no existe en {self.exchange.id}.")

        existing = self.exchange.fetch_open_orders(self.symbol)
        if existing:
            log.warning("Se encontraron %d órdenes previas en %s: se cancelan para iniciar una grilla limpia.",
                        len(existing), self.symbol)
            self.exchange.cancel_all_orders(self.symbol)

        equity = self.get_equity()
        self._update_daily_baseline(equity)
        quote_total, _ = self.get_balances()
        log.info("Arranque: equity %.2f USDT | USDT libre %.2f | umbral freno global %.2f USDT",
                 equity, quote_total, GLOBAL_STOP_USDT)
        if equity < GLOBAL_STOP_USDT:
            log.critical("El capital inicial ya está por debajo del freno global: se activa la pausa inmediata.")
            self._emergency_stop(equity)

    def run(self) -> None:
        """Bucle con manejo exhaustivo de excepciones: nunca debe crashear."""
        log.info("=" * 72)
        log.info("GRID BOT | ccxt %s | modo=%s | par=%s (lista blanca)",
                 ccxt.__version__, "TESTNET/DEMO" if self.settings.testnet else "REAL", self.symbol)
        log.info("Capital %.0f USDT = %.0f%% libre (%.2f USDT) + %.0f%% grilla (%.2f USDT) | %d celdas | spacing %.2f%%",
                 self.config.capital, FREE_ALLOCATION_PCT * 100, self.config.free_reserve,
                 GRID_ALLOCATION_PCT * 100, self.config.grid_budget,
                 self.config.levels, self.config.spacing * 100.0)
        self._bootstrap()

        while self._running:
            try:
                self.run_cycle()
                time.sleep(self.settings.poll_interval_seconds)
            except ccxt.NetworkError as exc:
                delay = min(60, 5 * 2)
                log.warning("NetworkError (temporal): %s — reintento en %ds.", exc, delay)
                time.sleep(delay)
            except ccxt.AuthenticationError as exc:
                log.critical("AuthenticationError: revisa API_KEY/API_SECRET. Deteniendo.", exc_info=True)
                log_auth_help(exc)
                self._running = False
            except ccxt.ExchangeError as exc:
                log.error("ExchangeError: %s — continúo con el siguiente ciclo.", exc, exc_info=True)
                time.sleep(self.settings.poll_interval_seconds)
            except KeyboardInterrupt:
                log.info("Interrupción manual (Ctrl+C): se detiene el bot (las órdenes de la grilla permanecen).")
                self._running = False
            except Exception:
                log.exception("Error inesperado (continuaré en el próximo ciclo).")
                time.sleep(self.settings.poll_interval_seconds)

        log.info("Grid bot detenido de forma ordenada.")


def main() -> int:
    """Punto de entrada: configuración -> exchange -> rejilla -> bucle."""
    try:
        settings = load_settings()
        grid_config = build_grid_config()
    except ConfigError as exc:
        print(f"[ERROR DE CONFIGURACIÓN] {exc}", file=sys.stderr)
        return 1

    setup_logging(settings)
    try:
        exchange = create_exchange(settings)
        bot = GridBot(settings, exchange, grid_config)
        bot.run()
        return 0
    except ConfigError as exc:
        log.error("Configuración inválida: %s", exc)
        return 1
    except ccxt.BaseError as exc:
        log.critical("Error fatal de ccxt durante el arranque: %s", exc, exc_info=True)
        if isinstance(exc, ccxt.AuthenticationError):
            log_auth_help(exc)
        return 1
    except KeyboardInterrupt:
        log.info("Saliendo...")
        return 0


if __name__ == "__main__":
    sys.exit(main())
