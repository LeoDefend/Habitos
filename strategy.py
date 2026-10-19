"""
strategy.py — LÓGICA DE DECISIÓN (modular y reemplazable)
===========================================================
El bucle principal NO contiene reglas de trading: solo llama a
`generate_signal()`. Para cambiar de estrategia basta con implementar
otra clase con el mismo contrato y sustituirla en `bot.py` o cambiar
`STRATEGY_NAME` en `.env`.

Estrategias disponibles:
  1) SimpleSmaCrossoverStrategy ("sma_crossover"): Cruce de dos SMAs.
  2) RsiStrategy ("rsi"): Sobreventa / sobrecompra con RSI.
  3) EmaMacdStrategy ("macd"): Cruce de histograma MACD / línea de señal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from config import Settings


class Signal(str, Enum):
    """Señales que la estrategia puede devolver al bucle principal."""
    OPEN_LONG = "open_long"
    OPEN_SHORT = "open_short"
    HOLD = "hold"


@dataclass(frozen=True)
class StrategyDecision:
    """Resultado de la decisión: señal + motivo (para trazabilidad en logs)."""
    signal: Signal
    reason: str
    fast_sma: float | None = None
    slow_sma: float | None = None
    indicator_value: float | None = None


# --------------------------------------------------------------------------- #
# Indicadores Técnicos                                                        #
# --------------------------------------------------------------------------- #
def sma(values: Sequence[float], period: int) -> float | None:
    """Media móvil simple de `period` valores; None si no hay suficientes datos."""
    if period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / period


def ema(values: Sequence[float], period: int) -> float | None:
    """Media móvil exponencial (EMA) de `period` valores."""
    if period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    current_ema = sum(values[:period]) / period
    for val in values[period:]:
        current_ema = (val - current_ema) * k + current_ema
    return current_ema


def rsi(values: Sequence[float], period: int = 14) -> float | None:
    """Relative Strength Index (RSI) con suavizado de Wilder."""
    if period <= 0 or len(values) < period + 1:
        return None

    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        if diff >= 0:
            gains.append(diff)
            losses.append(0.0)
        else:
            gains.append(0.0)
            losses.append(abs(diff))

    if len(gains) < period:
        return None

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(
    values: Sequence[float], fast_period: int = 12, slow_period: int = 26, signal_period: int = 9
) -> tuple[float, float, float] | None:
    """MACD: retorna tuple (macd_line, signal_line, histogram) o None."""
    if fast_period >= slow_period or len(values) < slow_period + signal_period:
        return None

    k_fast = 2.0 / (fast_period + 1.0)
    k_slow = 2.0 / (slow_period + 1.0)

    macd_series: list[float] = []

    ema_fast_curr = sum(values[:fast_period]) / fast_period
    for i in range(fast_period, slow_period):
        ema_fast_curr = (values[i] - ema_fast_curr) * k_fast + ema_fast_curr

    ema_slow_curr = sum(values[:slow_period]) / slow_period
    macd_series.append(ema_fast_curr - ema_slow_curr)

    for i in range(slow_period, len(values)):
        ema_fast_curr = (values[i] - ema_fast_curr) * k_fast + ema_fast_curr
        ema_slow_curr = (values[i] - ema_slow_curr) * k_slow + ema_slow_curr
        macd_series.append(ema_fast_curr - ema_slow_curr)

    if len(macd_series) < signal_period:
        return None

    signal_line = ema(macd_series, signal_period)
    if signal_line is None:
        return None

    macd_line = macd_series[-1]
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def atr(candles: Sequence[Sequence[float]], period: int = 14) -> float | None:
    """Average True Range (ATR) sobre velas [timestamp, open, high, low, close, volume]."""
    if period <= 0 or not candles or len(candles) < period + 1:
        return None

    true_ranges: list[float] = []
    for i in range(1, len(candles)):
        high = float(candles[i][2])
        low = float(candles[i][3])
        prev_close = float(candles[i - 1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    if len(true_ranges) < period:
        return None

    current_atr = sum(true_ranges[:period]) / period
    for tr in true_ranges[period:]:
        current_atr = (current_atr * (period - 1) + tr) / period

    return current_atr


# --------------------------------------------------------------------------- #
# Estrategias                                                                 #
# --------------------------------------------------------------------------- #
class SimpleSmaCrossoverStrategy:
    """Estrategia de prueba: cruce básico de dos medias móviles (muy sencilla a propósito)."""

    name = "sma_crossover"

    def __init__(self, fast: int = 9, slow: int = 21) -> None:
        if fast >= slow:
            raise ValueError(f"El periodo rápido ({fast}) debe ser menor que el lento ({slow})")
        self.fast = fast
        self.slow = slow

    def generate_signal(self, candles: Sequence[Sequence[float]]) -> StrategyDecision:
        """
        Args:
            candles: velas [timestamp, open, high, low, close, volume]
                     (salida directa de exchange.fetch_ohlcv).
        Returns:
            StrategyDecision con la señal y el motivo.
        """
        # 1) Validación de datos: nunca operar con velas insuficientes
        total = len(candles) if candles else 0
        if total < self.slow + 1:
            return StrategyDecision(Signal.HOLD, f"Velas insuficientes ({total} < {self.slow + 1})")

        # 2) Cálculo de indicadores (índice 4 = precio de cierre de la vela)
        closes: list[float] = [float(candle[4]) for candle in candles]
        fast_sma = sma(closes, self.fast)
        slow_sma = sma(closes, self.slow)
        if fast_sma is None or slow_sma is None:
            return StrategyDecision(Signal.HOLD, "No se pudieron calcular las SMAs")

        # 3) Regla de decisión (cruce de medias)
        last_close = closes[-1]
        if fast_sma > slow_sma:
            return StrategyDecision(
                Signal.OPEN_LONG,
                f"SMA{self.fast} ({fast_sma:.2f}) > SMA{self.slow} ({slow_sma:.2f}) | cierre {last_close:.2f}",
                fast_sma,
                slow_sma,
            )
        if fast_sma < slow_sma:
            return StrategyDecision(
                Signal.OPEN_SHORT,
                f"SMA{self.fast} ({fast_sma:.2f}) < SMA{self.slow} ({slow_sma:.2f}) | cierre {last_close:.2f}",
                fast_sma,
                slow_sma,
            )
        return StrategyDecision(
            Signal.HOLD,
            f"SMAs iguales/planas ({fast_sma:.2f} == {slow_sma:.2f})",
            fast_sma,
            slow_sma,
        )


class RsiStrategy:
    """Estrategia de impulso/reversión con Relative Strength Index (RSI)."""

    name = "rsi"

    def __init__(self, period: int = 14, overbought: float = 70.0, oversold: float = 30.0) -> None:
        if oversold >= overbought:
            raise ValueError(f"oversold ({oversold}) debe ser menor que overbought ({overbought})")
        self.period = period
        self.overbought = overbought
        self.oversold = oversold

    def generate_signal(self, candles: Sequence[Sequence[float]]) -> StrategyDecision:
        total = len(candles) if candles else 0
        if total < self.period + 2:
            return StrategyDecision(Signal.HOLD, f"Velas insuficientes ({total} < {self.period + 2})")

        closes: list[float] = [float(candle[4]) for candle in candles]
        val_rsi = rsi(closes, self.period)
        if val_rsi is None:
            return StrategyDecision(Signal.HOLD, "No se pudo calcular el RSI")

        last_close = closes[-1]
        if val_rsi <= self.oversold:
            return StrategyDecision(
                Signal.OPEN_LONG,
                f"RSI({self.period}) {val_rsi:.2f} <= sobreventa {self.oversold:.1f} | cierre {last_close:.2f}",
                indicator_value=val_rsi,
            )
        if val_rsi >= self.overbought:
            return StrategyDecision(
                Signal.OPEN_SHORT,
                f"RSI({self.period}) {val_rsi:.2f} >= sobrecompra {self.overbought:.1f} | cierre {last_close:.2f}",
                indicator_value=val_rsi,
            )
        return StrategyDecision(
            Signal.HOLD,
            f"RSI({self.period}) {val_rsi:.2f} en zona neutral ({self.oversold:.1f}-{self.overbought:.1f})",
            indicator_value=val_rsi,
        )


class EmaMacdStrategy:
    """Estrategia de tendencia/momento con MACD (Moving Average Convergence Divergence)."""

    name = "macd"

    def __init__(self, fast: int = 12, slow: int = 26, signal: int = 9) -> None:
        if fast >= slow:
            raise ValueError(f"fast ({fast}) debe ser menor que slow ({slow})")
        self.fast = fast
        self.slow = slow
        self.signal = signal

    def generate_signal(self, candles: Sequence[Sequence[float]]) -> StrategyDecision:
        total = len(candles) if candles else 0
        min_candles = self.slow + self.signal + 2
        if total < min_candles:
            return StrategyDecision(Signal.HOLD, f"Velas insuficientes ({total} < {min_candles})")

        closes: list[float] = [float(candle[4]) for candle in candles]
        macd_res = macd(closes, self.fast, self.slow, self.signal)
        if macd_res is None:
            return StrategyDecision(Signal.HOLD, "No se pudo calcular el MACD")

        macd_line, signal_line, histogram = macd_res
        last_close = closes[-1]

        if histogram > 0 and macd_line > signal_line:
            return StrategyDecision(
                Signal.OPEN_LONG,
                f"MACD histograma alcista (+{histogram:.4f}) | MACD {macd_line:.2f} > Señal {signal_line:.2f} | cierre {last_close:.2f}",
                indicator_value=histogram,
            )
        if histogram < 0 and macd_line < signal_line:
            return StrategyDecision(
                Signal.OPEN_SHORT,
                f"MACD histograma bajista ({histogram:.4f}) | MACD {macd_line:.2f} < Señal {signal_line:.2f} | cierre {last_close:.2f}",
                indicator_value=histogram,
            )
        return StrategyDecision(
            Signal.HOLD,
            f"MACD sin sesgo claro (histograma {histogram:.4f})",
            indicator_value=histogram,
        )


def create_strategy(settings: Settings) -> Any:
    """Factoría de estrategias a partir de la configuración cargada."""
    name = settings.strategy_name.lower()
    if name == "sma_crossover":
        return SimpleSmaCrossoverStrategy(settings.sma_fast, settings.sma_slow)
    if name == "rsi":
        return RsiStrategy(settings.rsi_period, settings.rsi_overbought, settings.rsi_oversold)
    if name == "macd":
        return EmaMacdStrategy(settings.macd_fast, settings.macd_slow, settings.macd_signal)
    raise ValueError(f"Estrategia no reconocida: {settings.strategy_name!r}")
