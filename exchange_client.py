"""
exchange_client.py — CONEXIÓN AL EXCHANGE EN MODO TESTNET / DEMO (Paper Trading)
=================================================================================
Responsabilidades:
  1. Instanciar el exchange de ccxt con las credenciales cargadas del .env.
  2. FORZAR el entorno de pruebas (sandbox / demo) antes de cualquier llamada.
  3. Respetar los rate-limits del exchange (enableRateLimit=True).

Nota de versiones de ccxt (comprobado en los issues #27039 y #27266 del repo):
  * ccxt >= 4.5 recomienda `enable_demo_trading(True)` (sustituye a set_sandbox_mode,
    que quedó obsoleto para Binance; las claves de testnet se crean en
    https://testnet.binancefuture.com (futuros) o https://testnet.binance.vision (spot)).
  * Versiones antiguas usaban `set_sandbox_mode(True)`.
Soportamos ambas para no depender de una versión exacta de ccxt.
"""

from __future__ import annotations

import logging

import ccxt

from config import ConfigError, Settings

log = logging.getLogger("exchange")


def create_exchange(settings: Settings) -> ccxt.Exchange:
    """Instancia el exchange, activa el modo testnet y deja todo listo para operar."""
    if not hasattr(ccxt, settings.exchange_id):
        raise ConfigError(
            f"El exchange '{settings.exchange_id}' no existe en ccxt. "
            f"Ejemplos válidos: binanceusdm, binance, bybit, krakenfutures..."
        )

    exchange_class = getattr(ccxt, settings.exchange_id)

    # 'defaultType': ccxt necesita saber el tipo por defecto; 'swap' para perps (:USDT)
    default_type = "swap" if ":" in settings.symbol else "spot"

    exchange = exchange_class(
        {
            "apiKey": settings.api_key,      # <- desde .env (NUNCA hardcodeado)
            "secret": settings.api_secret,   # <- desde .env (NUNCA hardcodeado)
            "enableRateLimit": True,         # respeta los rate-limits del exchange
            "options": {
                "defaultType": default_type,
                # Sincroniza el reloj del equipo con el servidor del exchange: ccxt
                # mide el desfase (offset) con fetch_time y lo suma a los timestamps
                # de cada petición firmada. Evita errores de firma/"timestamp fuera
                # de ventana" aunque el PC tenga la hora desincronizada.
                "adjustForTimeDifference": True,
                # Anti -1021: margen de tolerancia del timestamp en milisegundos.
                # Binance admite como máximo 60000 (más genera -1131); ccxt por defecto
                # usa 10000, que es justo si el reloj deriva entre re-sincronizaciones.
                "recvWindow": settings.recv_window_ms,
            },
        }
    )

    enable_testnet_mode(exchange, settings)
    return exchange


def enable_testnet_mode(exchange: ccxt.Exchange, settings: Settings) -> None:
    """Fuerza el entorno de pruebas (paper trading) o aborta si no está permitido real.

    TESTNET_MODE controla a qué endpoint de pruebas se apunta (IMPORTANTE en Binance,
    que separó los portales y cada uno emite claves distintas):
      * "sandbox" -> set_sandbox_mode(True)     (endpoint antiguo: testnet.binance.vision /
                                                 testnet.binancefuture.com)
      * "demo"    -> enable_demo_trading(True)  (demo nuevo: demo-api.binance.com,
                                                 claves de demo.binance.com)
      * "auto"    -> demo si el método existe, si no sandbox (por defecto)
    """
    if not settings.testnet:
        # Doble llave: se necesitan TESTNET=false Y ALLOW_LIVE=true para salir del sandbox
        if not settings.allow_live:
            raise ConfigError("TESTNET=false y ALLOW_LIVE=false: el bot se niega a operar en real.")
        log.critical("MODO REAL ACTIVADO — se operará con dinero propio en %s.", exchange.id)
        return

    has_demo = hasattr(exchange, "enable_demo_trading")
    has_sandbox = hasattr(exchange, "set_sandbox_mode")
    mode = settings.testnet_mode

    if mode == "sandbox":
        if not has_sandbox:
            raise ConfigError(f"'{exchange.id}' no soporta set_sandbox_mode en esta versión de ccxt.")
        exchange.set_sandbox_mode(True)
        applied = "set_sandbox_mode(True)"
    elif mode == "demo":
        if not has_demo:
            raise ConfigError(f"'{exchange.id}' no soporta enable_demo_trading en esta versión de ccxt.")
        exchange.enable_demo_trading(True)
        applied = "enable_demo_trading(True)"
    else:  # "auto"
        if has_demo:
            exchange.enable_demo_trading(True)
            applied = "enable_demo_trading(True)"
        elif has_sandbox:
            exchange.set_sandbox_mode(True)
            applied = "set_sandbox_mode(True)"
        else:
            raise ConfigError(
                f"El exchange '{exchange.id}' no expone modo sandbox/demo en esta versión de ccxt."
            )

    # Loguear el endpoint REAL al que apuntamos: primera línea de diagnóstico ante -2015
    endpoint = exchange.urls.get("api", {})
    if isinstance(endpoint, dict):
        endpoint = endpoint.get("private") or endpoint.get("public") or str(endpoint)
    log.info("Modo TESTNET/DEMO activo en %s (%s | TESTNET_MODE=%s).", exchange.id, applied, mode)
    log.info("Endpoint API activo: %s", endpoint)
