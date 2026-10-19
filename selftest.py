"""
selftest.py — Verificación OFFLINE de la lógica de riesgo, estrategias y grilla.
Ejecutar:  python selftest.py
Comprueba las matemáticas que protegen el capital antes de operar en el testnet.
"""

from __future__ import annotations

import datetime as dt
import types

import ccxt

from grid_bot import CAPITAL_USDT, GridBot, GridConfig
from risk_manager import RiskManager, RiskRejection
from strategy import (
    EmaMacdStrategy,
    RsiStrategy,
    Signal,
    SimpleSmaCrossoverStrategy,
    atr,
    create_strategy,
    ema,
    macd,
    rsi,
    sma,
)


def make_risk(**overrides) -> RiskManager:
    params = dict(
        risk_per_trade_pct=1.0,
        max_daily_drawdown_pct=2.0,
        stop_loss_pct=1.0,
        take_profit_pct=2.0,
        max_notional_pct_of_equity=50.0,
        max_concurrent_positions=1,
    )
    params.update(overrides)
    return RiskManager(**params)


def test_sl_tp() -> None:
    risk = make_risk()
    sl, tp = risk.calculate_stop_loss_take_profit("long", 100.0)
    assert abs(sl - 99.0) < 1e-9 and abs(tp - 102.0) < 1e-9, (sl, tp)
    sl, tp = risk.calculate_stop_loss_take_profit("short", 100.0)
    assert abs(sl - 101.0) < 1e-9 and abs(tp - 98.0) < 1e-9, (sl, tp)
    print("OK  SL/TP porcentuales correctos para long y short")


def test_atr_sl_tp() -> None:
    risk = make_risk(use_atr_sl_tp=True, atr_sl_mult=1.5, atr_tp_mult=3.0)
    sl, tp = risk.calculate_stop_loss_take_profit("long", 100.0, atr_value=2.0)
    assert abs(sl - 97.0) < 1e-9 and abs(tp - 106.0) < 1e-9, (sl, tp)
    sl, tp = risk.calculate_stop_loss_take_profit("short", 100.0, atr_value=2.0)
    assert abs(sl - 103.0) < 1e-9 and abs(tp - 94.0) < 1e-9, (sl, tp)
    print("OK  SL/TP dinámicos basados en ATR correctos")


def test_position_sizing() -> None:
    risk = make_risk()
    plan = risk.calculate_position_size(
        side="long", entry_price=100_000.0, equity=1_000.0, available_quote=10_000.0
    )
    assert abs(plan.quantity - 0.005) < 1e-12, plan.quantity
    assert abs(plan.risk_pct_of_equity - 0.5) < 1e-9, plan.risk_pct_of_equity

    plan2 = risk.calculate_position_size(
        side="long", entry_price=100_000.0, equity=1_000.0, available_quote=100.0
    )
    assert abs(plan2.quantity - 0.001) < 1e-12, plan2.quantity

    try:
        risk.calculate_position_size(
            side="long", entry_price=100_000.0, equity=1_000.0,
            available_quote=10_000.0, min_notional=1500.0,
        )
    except RiskRejection:
        print("OK  Position sizing (tope de riesgo, notional, saldo y mínimo)")
        return
    raise AssertionError("Se esperaba RiskRejection por notional mínimo")


def test_daily_drawdown() -> None:
    risk = make_risk()
    today = dt.date.today()
    risk.update_daily_baseline(1_000.0, today)
    assert risk.check_daily_drawdown(985.0) is True
    assert risk.check_daily_drawdown(979.9) is False
    assert risk.halted is True and "2.00%" in risk.halt_reason

    assert risk.check_daily_drawdown(2_000.0) is False
    risk.update_daily_baseline(5_000.0, today + dt.timedelta(days=1))
    allowed, reason = risk.can_open_position(0)
    assert allowed is False and "bloqueado" in reason.lower()
    print("OK  Max Daily Drawdown bloquea a los 2% y es permanente")


def test_zero_equity_no_crash() -> None:
    risk = make_risk()
    today = dt.date.today()
    risk.update_daily_baseline(0.0, today)
    assert risk.check_daily_drawdown(0.0) is True
    assert risk.halted is False

    risk.update_daily_baseline(10.0, today)
    assert abs(risk.daily_pnl(10.0)) < 1e-9
    assert risk.check_daily_drawdown(9.95) is True
    assert risk.check_daily_drawdown(9.79) is False
    print("OK  Equity 0 sin crash (cero división) y baseline anclado al primer equity positivo")


def test_min_notional_bump() -> None:
    risk = make_risk()
    plan = risk.calculate_position_size(
        side="long", entry_price=81_000.0, equity=10.0, available_quote=10.0,
        min_notional=5.0, amount_step=1e-05,
    )
    assert plan.quantity >= 8e-05 - 1e-12, plan.quantity
    assert plan.notional + 1e-9 >= 5.0, plan.notional
    assert plan.risk_amount <= 10.0 * 0.01 + 1e-9

    try:
        risk.calculate_position_size(
            side="long", entry_price=81_000.0, equity=5.0, available_quote=5.0,
            min_notional=5.0, amount_step=1e-05,
        )
    except RiskRejection:
        print("OK  Mínimo del mercado: se sube al mínimo si cabe en el riesgo; si no, se rechaza")
        return
    raise AssertionError("Se esperaba RiskRejection con equity=5")


def test_sma_strategy() -> None:
    strategy = SimpleSmaCrossoverStrategy(fast=3, slow=5)
    def candles(prices):
        return [[i, p, p, p, p, 1.0] for i, p in enumerate(prices)]
    assert strategy.generate_signal(candles([1, 2, 3, 4, 5, 6])).signal is Signal.OPEN_LONG
    assert strategy.generate_signal(candles([6, 5, 4, 3, 2, 1])).signal is Signal.OPEN_SHORT
    assert strategy.generate_signal(candles([1, 2])).signal is Signal.HOLD
    print("OK  Estrategia SMA: long, short y hold con datos insuficientes")


def test_rsi_strategy() -> None:
    strategy = RsiStrategy(period=3, overbought=70.0, oversold=30.0)
    def candles(prices):
        return [[i, p, p, p, p, 1.0] for i, p in enumerate(prices)]

    # Caída continua -> RSI bajo (sobreventa) -> OPEN_LONG
    res_long = strategy.generate_signal(candles([100, 90, 80, 70, 60]))
    assert res_long.signal is Signal.OPEN_LONG, res_long

    # Subida continua -> RSI alto (sobrecompra) -> OPEN_SHORT
    res_short = strategy.generate_signal(candles([10, 20, 30, 40, 50]))
    assert res_short.signal is Signal.OPEN_SHORT, res_short

    print("OK  Estrategia RSI: sobreventa (long) y sobrecompra (short)")


def test_macd_strategy() -> None:
    strategy = EmaMacdStrategy(fast=3, slow=5, signal=3)
    def candles(prices):
        return [[i, p, p, p, p, 1.0] for i, p in enumerate(prices)]

    # Tendencia alcista
    res_long = strategy.generate_signal(candles([10, 12, 15, 18, 22, 26, 30, 35, 40, 45]))
    assert res_long.signal in {Signal.OPEN_LONG, Signal.HOLD}, res_long

    print("OK  Estrategia MACD: generación de señales basada en histograma")


def test_strategy_factory() -> None:
    s_sma = types.SimpleNamespace(strategy_name="sma_crossover", sma_fast=9, sma_slow=21)
    s_rsi = types.SimpleNamespace(strategy_name="rsi", rsi_period=14, rsi_overbought=70.0, rsi_oversold=30.0)
    s_macd = types.SimpleNamespace(strategy_name="macd", macd_fast=12, macd_slow=26, macd_signal=9)

    assert isinstance(create_strategy(s_sma), SimpleSmaCrossoverStrategy)
    assert isinstance(create_strategy(s_rsi), RsiStrategy)
    assert isinstance(create_strategy(s_macd), EmaMacdStrategy)
    print("OK  Factoría de estrategias instancia la clase seleccionada")


# --------------------------------------------------------------------------- #
# Simulación del Grid Bot con Fake Exchange                                   #
# --------------------------------------------------------------------------- #
class FakeGridExchange:
    id = "fake"

    def __init__(self):
        self.usdt_free = CAPITAL_USDT
        self.usdt_locked = 0.0
        self.sol_free = 0.0
        self.sol_locked = 0.0
        self.price = 100.0
        self.orders: dict[str, dict] = {}

    def market(self, s):
        return {
            "id": "SOLUSDT", "symbol": "SOL/USDT", "base": "SOL", "quote": "USDT",
            "spot": True, "swap": False, "future": False, "contract": False,
            "limits": {"amount": {"min": 0.01, "step": 0.01}, "cost": {"min": 5.0}, "notional": {"min": 5.0}},
            "precision": {"amount": 0.01, "price": 0.01},
        }

    def fetch_ticker(self, s, params={}):
        return {"last": self.price}

    def amount_to_precision(self, s, a):
        if float(a) < 0.01:
            raise ccxt.InvalidOrder(f"amount {a} too small")
        return f"{float(a):.2f}"

    def price_to_precision(self, s, p):
        return f"{float(p):.2f}"

    def fetch_balance(self, params={}):
        return {
            "USDT": {"free": self.usdt_free, "used": self.usdt_locked, "total": self.usdt_free + self.usdt_locked},
            "SOL": {"free": self.sol_free, "used": self.sol_locked, "total": self.sol_free + self.sol_locked},
        }

    def _process_fills(self):
        fee = 0.001
        for oid, o in self.orders.items():
            if o["status"] != "open":
                continue
            if o["side"] == "buy" and self.price <= o["price"]:
                o["status"] = "closed"
                self.usdt_locked -= o["usdt"]
                self.sol_free += o["qty"] * (1 - fee)
            elif o["side"] == "sell" and self.price >= o["price"]:
                o["status"] = "closed"
                self.sol_locked -= o["qty"]
                self.usdt_free += o["usdt"] * (1 - fee)

    def fetch_open_orders(self, symbol=None, params={}):
        self._process_fills()
        return [{"id": oid} for oid, o in self.orders.items() if o["status"] == "open"]

    def fetch_order(self, oid, symbol=None, params={}):
        self._process_fills()
        o = self.orders[str(oid)]
        return {"id": oid, "status": o["status"], "filled": o["qty"] if o["status"] == "closed" else 0.0}

    def create_order(self, symbol, type, side, amount, price=None, params={}):
        amount = float(amount)
        fee = 0.001
        if type == "market" and side == "sell":
            if amount > self.sol_free + 1e-12:
                raise ccxt.InsufficientFunds("market sell > free")
            self.sol_free -= amount
            self.usdt_free += amount * self.price * (1 - fee)
        elif type == "limit" and side == "buy":
            usdt = amount * float(price)
            if usdt > self.usdt_free + 1e-9:
                raise ccxt.InsufficientFunds("limit buy sin USDT libre")
            self.usdt_free -= usdt
            self.usdt_locked += usdt
        elif type == "limit" and side == "sell":
            if amount > self.sol_free + 1e-12:
                raise ccxt.InsufficientFunds("limit sell > free")
            self.sol_free -= amount
            self.sol_locked += amount
        oid = f"O-{len(self.orders) + 1}"
        self.orders[oid] = {
            "side": side,
            "price": float(price) if price else self.price,
            "qty": amount,
            "usdt": amount * (float(price) if price else self.price),
            "status": "open",
        }
        return {"id": oid}

    def cancel_all_orders(self, symbol=None, params={}):
        self.usdt_free += self.usdt_locked
        self.usdt_locked = 0.0
        self.sol_free += self.sol_locked
        self.sol_locked = 0.0
        for o in self.orders.values():
            if o["status"] == "open":
                o["status"] = "cancelled"
        return []


def test_grid_bot_simulation() -> None:
    fake_settings = types.SimpleNamespace(testnet=True, poll_interval_seconds=0)
    config = GridConfig(
        symbol="SOL/USDT", levels=8, spacing=0.01,
        capital=CAPITAL_USDT, grid_budget=CAPITAL_USDT * 0.40, free_reserve=CAPITAL_USDT * 0.60
    )
    fake = FakeGridExchange()
    bot = GridBot(fake_settings, fake, config)

    # 1. Grilla inicial
    bot.run_cycle()
    compras = [o for o in fake.orders.values() if o["side"] == "buy"]
    assert len(compras) == 5, len(compras)
    assert all(o["qty"] * o["price"] >= 5.0 - 1e-9 for o in compras)

    # 2. Caída de precio -> ejecuciones
    fake.price = 97.9
    bot.run_cycle()
    assert bot._cells_filled == 2, bot._cells_filled

    # 3. Rebote de precio -> TP
    fake.price = 100.0
    bot.run_cycle()
    assert len(bot._sell_orders) == 0

    # 4. Caída a 90 -> ejecuciones de las celdas de la grilla
    fake.price = 90.0
    bot.run_cycle()
    assert bot._cells_filled == 7, bot._cells_filled

    # 5. Caída por debajo del freno global (< 95 USDT de capital)
    fake.price = 84.0
    bot.run_cycle()
    assert bot._halted is True
    assert fake.sol_locked == 0.0

    print("OK  Simulación completa del Grid Bot (ciclo de vida y freno global)")


if __name__ == "__main__":
    test_sl_tp()
    test_atr_sl_tp()
    test_position_sizing()
    test_daily_drawdown()
    test_zero_equity_no_crash()
    test_min_notional_bump()
    test_sma_strategy()
    test_rsi_strategy()
    test_macd_strategy()
    test_strategy_factory()
    test_grid_bot_simulation()
    print("\nTodos los tests (riesgo, estrategias y grid) pasaron exitosamente ✔")
