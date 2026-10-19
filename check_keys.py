"""
check_keys.py — Diagnóstico de credenciales SIN RIESGO (solo lectura, nunca coloca órdenes).
Uso:  python check_keys.py
Comprueba en qué entorno de Binance es válida la API_KEY de tu .env y con qué saldo.
Úsalo tras crear o editar una key para confirmar que el bot podrá arrancar.
"""
import os
import re

import ccxt
from dotenv import load_dotenv

load_dotenv(dotenv_path=".env", override=False)
key = os.getenv("API_KEY", "")
secret = os.getenv("API_SECRET", "")
print("Key actual:", (key[:4] + "…" + key[-4:]) if len(key) >= 8 else "(no definida)", f"(longitud {len(key)})")
print()


def code_of(exc: Exception) -> str:
    match = re.search(r'"code"\s*:\s*(-?\d+)', str(exc))
    return match.group(1) if match else "?"


def probe(exchange_id: str, mode: str | None, label: str) -> bool:
    # Mismas opciones de sincronización que el bot (previene -1021 por reloj desfasado)
    ex = getattr(ccxt, exchange_id)({
        "apiKey": key,
        "secret": secret,
        "enableRateLimit": True,
        "options": {"adjustForTimeDifference": True, "recvWindow": 60000},
    })
    if mode == "demo":
        ex.enable_demo_trading(True)
    elif mode == "sandbox":
        ex.set_sandbox_mode(True)
    try:
        balance = ex.fetch_balance()
        usdt = balance.get("USDT", {})
        print(f"  *** AUTENTICADO *** | {label} | USDT free={usdt.get('free')} total={usdt.get('total')}")
        return True
    except ccxt.AuthenticationError as exc:
        print(f"  falla (codigo {code_of(exc)}) | {label}")
    except Exception as exc:
        print(f"  otro error | {label} | {type(exc).__name__}: {str(exc)[:50]}")
    return False


print("== Probando la key en todos los entornos de Binance ==")
live = probe("binance", None, "PRODUCCION spot (api.binance.com)")
probe("binance", "sandbox", "spot testnet (testnet.binance.vision)")
probe("binance", "demo", "demo nuevo (demo-api.binance.com)")
probe("binanceusdm", "demo", "futuros testnet (testnet.binancefuture.com)")

print()
if live:
    print("RESULTADO: tus keys sirven para OPERAR EN REAL. Ya puedes ejecutar: python bot.py")
else:
    print("RESULTADO: tus keys NO autentican en producción. Códigos:")
    print("  -2008 = la key no existe (borrada/caducada/errónea) -> créala de nuevo en Binance.")
    print("  -2015 = la key existe pero la bloquea algo -> en API Management de Binance revisa:")
    print("          1) Permisos: 'Enable Reading' + 'Enable Spot & Margin Trading' activos.")
    print("          2) Restricción de IP: añade tu IP actual o quita la restricción.")
    print("          3) Que la key esté creada en la pestaña LIVE (no Demo).")
