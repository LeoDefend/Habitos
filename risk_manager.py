"""
risk_manager.py — GESTIÓN DE RIESGO OBLIGATORIA (toda verificada en código)
=============================================================================
Tres reglas INQUEBRANTABLES, validadas antes de CADA orden:

 1) POSITION SIZING: nunca se arriesga más de `risk_per_trade_pct` % del capital
    por operación:
        riesgo_usdt  = equity * risk_per_trade_pct / 100
        distancia_SL = |precio_entrada - stop_loss|      (pérdida por unidad)
        cantidad     = min( riesgo_usdt / distancia_SL , # limitado por riesgo
                            tope_notional / precio     , # tope duro de notional
                            saldo_disponible / precio  ) # nunca gastar de más

 2) STOP-LOSS / TAKE-PROFIT OBLIGATORIOS (porcentuales o dinámicos por ATR):
        LONG : SL = entrada - ATR*mult    TP = entrada + ATR*mult
        SHORT: SL = entrada + ATR*mult    TP = entrada - ATR*mult

 3) MAX DAILY DRAWDOWN: si el equity cae `max_daily_drawdown_pct` % respecto al
    equity del inicio del día natural, el gestor se BLOQUEA (halt permanente
    hasta reiniciar el bot) y el llamador DEBE cerrar posiciones y alertar.

Este módulo es puro (sin red ni exchange): se puede testear de forma aislada.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass


class RiskRejection(Exception):
    """La operación propuesta viola una regla de riesgo y NO debe ejecutarse."""


@dataclass(frozen=True)
class RiskPlan:
    """Plan de riesgo aprobado que acompaña a cada orden de entrada."""
    side: str                  # "long" | "short"
    entry_price: float
    quantity: float            # unidades del activo base
    notional: float            # quantity * entry_price (USDT)
    stop_loss_price: float
    take_profit_price: float
    risk_amount: float         # pérdida máxima REAL en USDT si se cumple el SL
    risk_pct_of_equity: float  # esa pérdida como % del capital


class RiskManager:
    """Aplica las tres reglas de riesgo anteriores de forma independiente al exchange."""

    def __init__(
        self,
        *,
        risk_per_trade_pct: float,
        max_daily_drawdown_pct: float,
        stop_loss_pct: float,
        take_profit_pct: float,
        max_notional_pct_of_equity: float,
        max_concurrent_positions: int,
        use_atr_sl_tp: bool = False,
        atr_sl_mult: float = 1.5,
        atr_tp_mult: float = 3.0,
    ) -> None:
        self.risk_per_trade_pct = risk_per_trade_pct
        self.max_daily_drawdown_pct = max_daily_drawdown_pct
        self.stop_loss_pct = stop_loss_pct
        self.take_profit_pct = take_profit_pct
        self.max_notional_pct_of_equity = max_notional_pct_of_equity
        self.max_concurrent_positions = max_concurrent_positions
        self.use_atr_sl_tp = use_atr_sl_tp
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mult = atr_tp_mult

        # Estado del bloqueo diario
        self._halted = False
        self._halt_reason = ""
        self._daily_date: dt.date | None = None
        self._daily_start_equity: float | None = None

    # ------------------------------------------------------------------ #
    # 3) LÍMITE DE PÉRDIDA DIARIA (MAX DAILY DRAWDOWN)                    #
    # ------------------------------------------------------------------ #
    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    def halt(self, reason: str) -> None:
        """Bloquea el bot. Solo se desbloquea reiniciando el proceso (decisión humana)."""
        self._halted = True
        self._halt_reason = reason

    def update_daily_baseline(self, equity: float, today: dt.date) -> None:
        """Fija el equity de referencia al cambiar de día natural.
        Solo se ancla con equity POSITIVO: una cuenta a 0 no sirve como referencia
        (evita divisiones por cero); si llegan fondos en el día, ese equity pasa a
        ser la referencia. Si el bot está bloqueado NO se reinicia el baseline."""
        if self._halted:
            return
        if self._daily_date != today:
            self._daily_date = today
            self._daily_start_equity = None
        if self._daily_start_equity is None and equity > 0:
            self._daily_start_equity = equity

    def daily_pnl(self, equity: float) -> float | None:
        """P&L acumulado del día (realizado + no realizado), en USDT."""
        if self._daily_start_equity is None:
            return None
        return equity - self._daily_start_equity

    def check_daily_drawdown(self, equity: float) -> bool:
        """True si aún es seguro operar; False si se superó el límite (y activa halt)."""
        if self._halted:
            return False
        if self._daily_start_equity is None or self._daily_start_equity <= 0:
            return True  # sin referencia positiva no hay drawdown que evaluar (evita división por 0)
        pnl = equity - self._daily_start_equity
        max_loss = self._daily_start_equity * (self.max_daily_drawdown_pct / 100.0)
        if pnl <= -max_loss:
            pct = (-pnl / self._daily_start_equity) * 100.0
            self.halt(
                f"Pérdida diaria de {abs(pnl):.2f} USDT ({pct:.2f}%) ≥ límite "
                f"{self.max_daily_drawdown_pct:.2f}% "
                f"(equity {equity:.2f} vs inicio de día {self._daily_start_equity:.2f})"
            )
            return False
        return True


    # ------------------------------------------------------------------ #
    # 2) STOP-LOSS / TAKE-PROFIT PORCENTUAL O DINÁMICO (ATR)             #
    # ------------------------------------------------------------------ #
    def calculate_stop_loss_take_profit(
        self, side: str, entry_price: float, atr_value: float | None = None
    ) -> tuple[float, float]:
        """Calcula los precios de SL y TP a partir de porcentaje o ATR dinámico."""
        if entry_price <= 0:
            raise RiskRejection(f"Precio de entrada inválido: {entry_price}")

        if self.use_atr_sl_tp and atr_value is not None and atr_value > 0:
            if side == "long":
                stop_loss = entry_price - atr_value * self.atr_sl_mult
                take_profit = entry_price + atr_value * self.atr_tp_mult
            elif side == "short":
                stop_loss = entry_price + atr_value * self.atr_sl_mult
                take_profit = entry_price - atr_value * self.atr_tp_mult
            else:
                raise RiskRejection(f"Dirección desconocida: {side!r} (esperado 'long' o 'short')")
        elif side == "long":
            # LONG: SL por debajo de la entrada, TP por encima
            stop_loss = entry_price * (1.0 - self.stop_loss_pct / 100.0)
            take_profit = entry_price * (1.0 + self.take_profit_pct / 100.0)
        elif side == "short":
            # SHORT: SL por encima de la entrada, TP por debajo
            stop_loss = entry_price * (1.0 + self.stop_loss_pct / 100.0)
            take_profit = entry_price * (1.0 - self.take_profit_pct / 100.0)
        else:
            raise RiskRejection(f"Dirección desconocida: {side!r} (esperado 'long' o 'short')")

        if stop_loss <= 0:
            raise RiskRejection(f"Stop-Loss calculado <= 0 ({stop_loss})")
        if take_profit <= 0 or take_profit == entry_price:
            raise RiskRejection(f"Take-Profit inválido ({take_profit})")
        return stop_loss, take_profit

    # ------------------------------------------------------------------ #
    # 1) POSITION SIZING (tamaño de posición)                             #
    # ------------------------------------------------------------------ #
    def calculate_position_size(
        self,
        *,
        side: str,
        entry_price: float,
        equity: float,
        available_quote: float,
        min_notional: float = 0.0,
        amount_step: float = 0.0,
        atr_value: float | None = None,
    ) -> RiskPlan:
        """Dimensiona la operación sin arriesgar más del % permitido por el capital."""
        if equity <= 0:
            raise RiskRejection("Equity no positivo: no se puede dimensionar la operación")
        if entry_price <= 0:
            raise RiskRejection(f"Precio de entrada inválido: {entry_price}")
        available_quote = max(available_quote, 0.0)

        stop_loss, take_profit = self.calculate_stop_loss_take_profit(side, entry_price, atr_value)

        # Pérdida por unidad si se alcanza el SL
        distance = abs(entry_price - stop_loss)
        if distance <= 0:
            raise RiskRejection("Distancia al Stop-Loss es 0: no se puede calcular tamaño")

        # (a) límite por RIESGO: presupuesto máximo arriesgado en esta operación
        risk_budget = equity * (self.risk_per_trade_pct / 100.0)
        qty_by_risk = risk_budget / distance

        # (b) límite duro de NOTIONAL (ej. 50% del capital como tope)
        max_notional = equity * (self.max_notional_pct_of_equity / 100.0)
        qty_by_notional = max_notional / entry_price

        # (c) límite por LIQUIDEZ disponible (nunca gastar más del saldo libre)
        qty_by_balance = available_quote / entry_price

        quantity = min(qty_by_risk, qty_by_notional, qty_by_balance)
        if quantity <= 0:
            raise RiskRejection(
                f"Tamaño calculado <= 0 (equity={equity:.2f}, disponible={available_quote:.2f})"
            )

        # MÍNIMO DEL MERCADO (ej. 5 USDT en BTC/USDT spot): si el tamaño queda por
        # debajo, se sube AL ALZA al mínimo redondeado al step del mercado, PERO solo
        # si el riesgo resultante sigue dentro del presupuesto; si no, se rechaza.
        # (Con tope de notional al 50%, hacen falta ~10 USDT de equity para operar.)
        if min_notional > 0:
            min_qty = min_notional / entry_price
            if amount_step > 0:
                # Redondeo AL ALZA al step + 1 step de colchón: en spot la venta de
                # protección (SL) usa el balance ya descontado por comisión y truncado
                # hacia abajo, y el notional de esa orden debe seguir superando el
                # mínimo del mercado o Binance la rechazaría.
                min_qty = math.ceil(min_qty / amount_step - 1e-9) * amount_step + amount_step
            if quantity < min_qty and min_qty * distance > risk_budget:
                raise RiskRejection(
                    f"Equity insuficiente para el mínimo del mercado: operar {min_notional:.2f} USDT "
                    f"exigiría arriesgar {min_qty * distance:.2f} USDT > presupuesto {risk_budget:.2f} USDT. "
                    f"Recarga la cuenta o usa un par con mínimo menor."
                )
            quantity = max(quantity, min_qty)

        notional = quantity * entry_price
        if min_notional and notional + 1e-9 < min_notional:
            raise RiskRejection(
                f"Notional {notional:.2f} USDT < mínimo del mercado {min_notional:.2f} USDT "
                f"(aumenta el capital o reduce STOP_LOSS_PCT)"
            )

        # Riesgo REAL de este plan (puede ser menor al presupuesto si otro límite mandó)
        risk_amount = quantity * distance
        return RiskPlan(
            side=side,
            entry_price=entry_price,
            quantity=quantity,
            notional=notional,
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            risk_amount=risk_amount,
            risk_pct_of_equity=(risk_amount / equity) * 100.0,
        )

    def can_open_position(self, open_positions: int) -> tuple[bool, str]:
        """¿Se permite abrir una posición ahora mismo? (bloqueo diario + límite de posiciones)."""
        if self._halted:
            return False, f"Bot bloqueado: {self._halt_reason}"
        if open_positions >= self.max_concurrent_positions:
            return False, (
                f"Posiciones abiertas {open_positions} == máximo permitido "
                f"{self.max_concurrent_positions}"
            )
        return True, ""
