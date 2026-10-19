# 🤖 Bot Tradin — Base de bot de trading en Testnet (Paper Trading con CCXT)

Estructura base, **modular, robusta y segura**, de un bot de trading algorítmico que opera
únicamente en el **entorno de pruebas (sandbox/demo)** de un exchange vía [CCXT](https://docs.ccxt.com/).
No arriesga dinero real: todas las órdenes van al testnet.

Incluye dos modalidades operativas:
1. **Trend / Momentum Bot (`bot.py`)**: Estrategias de tendencia (SMA, RSI, MACD) con gestión de riesgo estricta (position sizing, SL/TP fijos o dinámicos por ATR y Max Daily Drawdown).
2. **Grid Bot (`grid_bot.py`)**: Bot de rejilla (Grid Trading) para Spot Altcoins con autocap de niveles, verificación de notional mínimo (>= 5 USDT) y freno global de capital.

---

## ⚡ Instalación

```bat
:: 1. (Recomendado) crear entorno virtual
python -m venv .venv
.venv\Scripts\activate

:: 2. instalar dependencias
pip install -r requirements.txt

:: 3. crear tu archivo de configuración local
copy .env.example .env

:: 4. (opcional) probar la lógica de riesgo, estrategias y grilla sin red
python selftest.py

:: 5. arrancar el bot de tendencia
python bot.py

:: 6. o arrancar el bot de grilla
python grid_bot.py
```

En macOS/Linux: `source .venv/bin/activate` y `cp .env.example .env`.

**Claves de testnet (nunca uses claves reales):**

| Mercado | Dónde crear las claves | `EXCHANGE_ID` en `.env` |
|---|---|---|
| Futuros USD-M (perp) | https://testnet.binancefuture.com | `binanceusdm` |
| Spot | https://testnet.binance.vision | `binance` |

---

## 📁 Estructura

```
Bot Tradin/
├── .env.example        # plantilla de configuración (sin secretos)
├── .env                # TU configuración real con claves (no se versiona)
├── .gitignore          # excluye .env, logs y basura de Python
├── requirements.txt    # ccxt + python-dotenv
├── config.py           # 1) carga y VALIDA todo desde .env (fail-fast, cero hardcode)
├── exchange_client.py  # 2) conexión CCXT forzada a modo testnet/demo
├── risk_manager.py     # 3) gestión de riesgo: sizing, SL/TP (fijo / ATR) y drawdown diario
├── strategy.py         # 4) estrategias (SMA crossover, RSI, MACD + factoría + indicadores)
├── bot.py              # 5) bucle principal de tendencia + manejo de excepciones + alertas
├── grid_bot.py         # 6) bot de rejilla spot (Grid Trading) para altcoins de lista blanca
├── check_keys.py       # diagnóstico seguro de API keys en entornos de Binance
└── selftest.py         # suite completa de tests unitarios y simulación sin red
```

---

## 🛡️ Gestión de riesgo (todo verificado en código antes de cada orden)

1. **Position sizing (máx. 1% del capital por operación)**

   ```
   riesgo_usdt  = equity × RISK_PER_TRADE_PCT / 100
   distancia_SL = |entrada − stop_loss|              (pérdida por unidad)
   cantidad     = min( riesgo_usdt / distancia_SL,   # limitado por riesgo
                       tope_notional / precio,        # MAX_NOTIONAL_PCT_OF_EQUITY
                       saldo_disponible / precio )    # nunca gastar de más
   ```

2. **Stop-Loss y Take-Profit obligatorios** (porcentuales o dinámicos vía ATR):

   - **Porcentuales**:
     ```
     LONG : SL = entrada × (1 − SL%/100)   TP = entrada × (1 + TP%/100)
     SHORT: SL = entrada × (1 + SL%/100)   TP = entrada − (1 − TP%/100)
     ```
   - **Dinámicos (ATR)** (`USE_ATR_SL_TP=true`):
     ```
     LONG : SL = entrada − (ATR × ATR_SL_MULT)   TP = entrada + (ATR × ATR_TP_MULT)
     SHORT: SL = entrada + (ATR × ATR_SL_MULT)   TP = entrada − (ATR × ATR_TP_MULT)
     ```

   Entrada + SL + TP se ejecutan como **unidad atómica**: si la protección no se coloca,
   el bot hace *rollback* (cierra la entrada). Jamás deja posiciones sin SL.

3. **Filtro de polvo en Spot**:
   Evita que saldos residuales irrelevantes o invendibles (< 5 USDT / notional mínimo) bloqueen la ejecución de nuevas operaciones en spot.

4. **Max Daily Drawdown (máx. 2% de pérdida diaria)**
   Se compara el equity actual con el del inicio del día natural. Si la caída supera
   `MAX_DAILY_DRAWDOWN_PCT`: **cierra todas las posiciones, cancela órdenes, envía alerta
   (logs + webhook opcional) y se bloquea permanentemente** hasta reiniciar el bot manualmente.

   Bonus: `ENTRY_COOLDOWN_SECONDS` evita re-entradas inmediatas tras un stop-loss.

---

## 🧩 Estrategias disponibles (`STRATEGY_NAME` en `.env`)

1. **`sma_crossover`**: Cruce de Medias Móviles Simples (`SMA_FAST` vs `SMA_SLOW`).
2. **`rsi`**: Reversión por Relative Strength Index (`RSI_PERIOD`, `RSI_OVERBOUGHT`, `RSI_OVERSOLD`).
3. **`macd`**: Impulso / tendencia con Moving Average Convergence Divergence (`MACD_FAST`, `MACD_SLOW`, `MACD_SIGNAL`).

---

## 🔲 Grid Trading Bot (`grid_bot.py`)

Diseñado para operar en rango sobre altcoins de lista blanca (`SOL/USDT`, `LINK/USDT`, `ADA/USDT`, `AVAX/USDT`).

- **Autocap de celdas**: ajusta automáticamente el número de niveles para garantizar que cada orden supere los 5 USDT exigidos por Binance.
- **Ciclo atómico de celda**: al ejecutarse una compra límite, coloca automáticamente el Take-Profit (+spacing%) y re-arma la compra al venderse.
- **Freno Global de Capital**: si el equity total cae por debajo de 95 USDT (-5%), cancela la grilla, vende el saldo a mercado y se pausa permanentemente.

---

## 🩺 Troubleshooting: `Invalid API-key (-2015)`

Binance emite **claves distintas por portal de pruebas** y no son intercambiables:

| Portal donde creaste la key | Entorno real de la key | Configuración necesaria |
|---|---|---|
| https://testnet.binance.vision (spot, 10.000 USDT) | `https://testnet.binance.vision` | `TESTNET_MODE=sandbox` + `EXCHANGE_ID=binance` |
| https://demo.binance.com (demo nuevo) | `https://demo-api.binance.com` | `TESTNET_MODE=demo` + `EXCHANGE_ID=binance` |
| https://testnet.binancefuture.com (futuros) | `https://testnet.binancefuture.com` | `EXCHANGE_ID=binanceusdm` + `SYMBOL=BTC/USDT:USDT` |

Puedes ejecutar `python check_keys.py` para diagnosticar en qué entorno es válida tu API key.

---

## ⚠️ Notas y limitaciones

- El bloqueo por drawdown o freno global es **permanente hasta reiniciar el proceso** (decisión humana).
- `ALLOW_LIVE=true` + `TESTNET=false` es la única forma de salir del sandbox (doble llave).
- Disclaimer educativo: el trading con criptomonedas implica riesgo de pérdida; prueba y audita esta base en testnet antes de cualquier uso real.
