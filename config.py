"""
config.py — CONFIGURACIÓN Y CREDENCIALES (entorno TESTNET / PAPER TRADING)
===========================================================================
Paso 1 del bot: cargar la configuración desde el archivo `.env`.

Reglas de seguridad:
  * Las API keys NUNCA se escriben en el código fuente (se cargan con python-dotenv).
  * El archivo `.env` está en `.gitignore` (no se sube a ningún repositorio).
  * Todos los valores se validan aquí: si algo falta o es inválido, el bot
    FALLA RÁPIDO con un mensaje claro en lugar de operar con valores tontos.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Ruta raíz del proyecto (carpeta que contiene este archivo)
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"


class ConfigError(Exception):
    """Error irrecuperable de configuración (faltan variables o son inválidas)."""


# --------------------------------------------------------------------------- #
# Helpers de lectura/validación del .env                                       #
# --------------------------------------------------------------------------- #
def _require(name: str) -> str:
    """Variable obligatoria sin valor por defecto (usar solo para credenciales)."""
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"Falta la variable obligatoria '{name}' en {ENV_FILE}. "
            f"Copia '.env.example' a '.env' y complétala."
        )
    return value


def _get_float(name: str, default: float, *, minimum: float | None = None,
               maximum: float | None = None) -> float:
    raw = os.getenv(name, "").strip()
    try:
        value = float(raw) if raw else float(default)
    except ValueError as exc:
        raise ConfigError(f"'{name}' debe ser un número, recibido: {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"'{name}' debe ser >= {minimum} (recibido: {value})")
    if maximum is not None and value > maximum:
        raise ConfigError(f"'{name}' debe ser <= {maximum} (recibido: {value})")
    return value


def _get_int(name: str, default: int, *, minimum: int | None = None) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(float(raw)) if raw else int(default)
    except ValueError as exc:
        raise ConfigError(f"'{name}' debe ser un entero, recibido: {raw!r}") from exc
    if minimum is not None and value < minimum:
        raise ConfigError(f"'{name}' debe ser >= {minimum} (recibido: {value})")
    return value


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "y", "si", "sí", "on"}:
        return True
    if raw in {"0", "false", "no", "n", "off"}:
        return False
    raise ConfigError(f"'{name}' debe ser true/false, recibido: {raw!r}")


# --------------------------------------------------------------------------- #
# Settings: objeto inmutable con TODA la configuración del bot                 #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Settings:
    # --- Credenciales de TESTNET (solo viven en .env, jamás en el código) ---
    api_key: str
    api_secret: str
    # --- Exchange y mercado ---
    exchange_id: str               # p.ej. binanceusdm (futuros) o binance (spot)
    symbol: str                    # p.ej. BTC/USDT:USDT
    timeframe: str                 # p.ej. 5m
    # --- Riesgo obligatorio (valores en %) ---
    risk_per_trade_pct: float         # máximo % del capital arriesgado por operación
    max_daily_drawdown_pct: float     # pérdida diaria que BLOQUEA el bot
    stop_loss_pct: float              # SL porcentual desde la entrada
    take_profit_pct: float            # TP porcentual desde la entrada
    max_notional_pct_of_equity: float # tope duro de notional por operación
    max_concurrent_positions: int
    use_atr_sl_tp: bool               # usar ATR dinámico para SL/TP en vez de % fijo
    atr_period: int
    atr_sl_mult: float
    atr_tp_mult: float
    # --- Estrategias ---
    strategy_name: str                # "sma_crossover" | "rsi" | "macd"
    sma_fast: int
    sma_slow: int
    rsi_period: int
    rsi_overbought: float
    rsi_oversold: float
    macd_fast: int
    macd_slow: int
    macd_signal: int
    # --- Operación ---
    poll_interval_seconds: int
    entry_cooldown_seconds: int
    recv_window_ms: int              # recvWindow de Binance (márgen de tolerancia del timestamp)
    clock_resync_seconds: int        # cada cuántos segundos se re-sincroniza el reloj con el exchange
    testnet: bool                  # debe ser true: este bot es paper trading
    testnet_mode: str              # "auto" | "demo" (demo-api.binance.com) | "sandbox" (testnet.binance.*)
    allow_live: bool               # "break-glass": se necesitan DOS llaves para real
    alert_webhook_url: str         # opcional: webhook de Slack/Discord para alertas
    log_file: str




def load_settings() -> Settings:
    """Carga el .env, parsea y valida. Lanza ConfigError ante cualquier problema."""
    if not ENV_FILE.exists():
        raise ConfigError(
            f"No existe el archivo {ENV_FILE}. "
            f"Crea uno con: copy .env.example .env  (Windows) y añade tus claves de testnet."
        )

    # Carga las variables del .env sin sobreescribir las ya presentes en el entorno
    load_dotenv(dotenv_path=ENV_FILE, override=False)

    settings = Settings(
        # Credenciales: OBLIGATORIAS y nunca con valores por defecto
        api_key=_require("API_KEY"),
        api_secret=_require("API_SECRET"),
        # Exchange / mercado
        exchange_id=os.getenv("EXCHANGE_ID", "binanceusdm").strip().lower(),
        symbol=os.getenv("SYMBOL", "BTC/USDT:USDT").strip().upper(),
        timeframe=os.getenv("TIMEFRAME", "5m").strip(),
        # Riesgo
        risk_per_trade_pct=_get_float("RISK_PER_TRADE_PCT", 1.0, minimum=0.01, maximum=100.0),
        max_daily_drawdown_pct=_get_float("MAX_DAILY_DRAWDOWN_PCT", 2.0, minimum=0.01, maximum=100.0),
        stop_loss_pct=_get_float("STOP_LOSS_PCT", 1.0, minimum=0.01, maximum=100.0),
        take_profit_pct=_get_float("TAKE_PROFIT_PCT", 2.0, minimum=0.01),
        max_notional_pct_of_equity=_get_float("MAX_NOTIONAL_PCT_OF_EQUITY", 50.0, minimum=1.0, maximum=100.0),
        max_concurrent_positions=_get_int("MAX_CONCURRENT_POSITIONS", 1, minimum=1),
        # Riesgo dinámico ATR
        use_atr_sl_tp=_get_bool("USE_ATR_SL_TP", False),
        atr_period=_get_int("ATR_PERIOD", 14, minimum=2),
        atr_sl_mult=_get_float("ATR_SL_MULT", 1.5, minimum=0.1),
        atr_tp_mult=_get_float("ATR_TP_MULT", 3.0, minimum=0.1),
        # Estrategias
        strategy_name=os.getenv("STRATEGY_NAME", "sma_crossover").strip().lower(),
        sma_fast=_get_int("SMA_FAST", 9, minimum=2),
        sma_slow=_get_int("SMA_SLOW", 21, minimum=3),
        rsi_period=_get_int("RSI_PERIOD", 14, minimum=2),
        rsi_overbought=_get_float("RSI_OVERBOUGHT", 70.0, minimum=50.0, maximum=100.0),
        rsi_oversold=_get_float("RSI_OVERSOLD", 30.0, minimum=0.0, maximum=50.0),
        macd_fast=_get_int("MACD_FAST", 12, minimum=2),
        macd_slow=_get_int("MACD_SLOW", 26, minimum=3),
        macd_signal=_get_int("MACD_SIGNAL", 9, minimum=1),
        # Operación
        poll_interval_seconds=_get_int("POLL_INTERVAL_SECONDS", 60, minimum=5),
        entry_cooldown_seconds=_get_int("ENTRY_COOLDOWN_SECONDS", 300, minimum=0),
        recv_window_ms=_get_int("RECV_WINDOW_MS", 60000, minimum=1000),
        clock_resync_seconds=_get_int("CLOCK_RESYNC_SECONDS", 1800, minimum=60),
        testnet=_get_bool("TESTNET", True),
        testnet_mode=os.getenv("TESTNET_MODE", "auto").strip().lower(),
        allow_live=_get_bool("ALLOW_LIVE", False),
        alert_webhook_url=os.getenv("ALERT_WEBHOOK_URL", "").strip(),
        log_file=os.getenv("LOG_FILE", "bot.log").strip(),
    )

    # --- Validaciones cruzadas ---
    valid_strategies = {"sma_crossover", "rsi", "macd"}
    if settings.strategy_name not in valid_strategies:
        raise ConfigError(
            f"STRATEGY_NAME inválida: {settings.strategy_name!r} (opciones: {', '.join(sorted(valid_strategies))})."
        )
    if settings.testnet_mode not in {"auto", "demo", "sandbox"}:
        raise ConfigError(
            f"TESTNET_MODE inválido: {settings.testnet_mode!r} (usa 'auto', 'demo' o 'sandbox')."
        )
    if settings.sma_fast >= settings.sma_slow:
        raise ConfigError(
            f"SMA_FAST ({settings.sma_fast}) debe ser menor que SMA_SLOW ({settings.sma_slow})."
        )
    if settings.macd_fast >= settings.macd_slow:
        raise ConfigError(
            f"MACD_FAST ({settings.macd_fast}) debe ser menor que MACD_SLOW ({settings.macd_slow})."
        )
    if settings.rsi_oversold >= settings.rsi_overbought:
        raise ConfigError(
            f"RSI_OVERSOLD ({settings.rsi_oversold}) debe ser menor que RSI_OVERBOUGHT ({settings.rsi_overbought})."
        )
    if not settings.testnet and not settings.allow_live:
        raise ConfigError(
            "TESTNET=false exige ALLOW_LIVE=true (doble confirmación) para operar dinero real. "
            "Este bot está diseñado para paper trading en testnet."
        )
    if not settings.testnet:
        # Última barrera: avisar muy ruidosamente antes de tocar dinero real
        print("¡ATENCIÓN!: vas a operar en MERCADO REAL con dinero propio.", flush=True)
    return settings
