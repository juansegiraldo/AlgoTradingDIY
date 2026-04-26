# 10X Trading System

Crypto Spot GBP via Kraken | Forex | ETFs

**Estado actual:** Kraken Spot en GBP, modo `paper`, sin apalancamiento crypto.

> ADVERTENCIA: este sistema es un proyecto educativo y no constituye asesoramiento financiero.
> Crypto puede perder valor rapidamente. No hay garantia de beneficio ni de "richness".

## Estado Operativo

- Exchange crypto activo: `kraken`
- Pares crypto activos: `BTC/GBP`, `ETH/GBP`, `SOL/GBP`
- Modo por defecto: `paper`
- Crypto Spot solamente: no margin, no derivatives, no shorts, no withdrawals
- Ordenes reales: bloqueadas hasta pasar readiness en `semi_auto`

## Quick Start

```bash
# 1. Crear entorno virtual
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows PowerShell/cmd

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. Ejecutar tests
python -c "import os, pytest; os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'; raise SystemExit(pytest.main(['-q']))"

# 4. Resetear estado local antes de arrancar Kraken desde cero
python scripts/reset_runtime_state.py --confirm

# 5. Probar scanner con Kraken
python main.py --scan

# 6. Arrancar el bot en modo paper
python main.py
```

## Kraken API

La configuracion local va en `config/secrets.yaml`:

```yaml
kraken:
  api_key: "TU_API_KEY"
  api_secret: "TU_API_SECRET"
```

Tambien se puede usar variables de entorno:

```bash
KRAKEN_API_KEY=...
KRAKEN_API_SECRET=...
```

Permisos requeridos para la API key de Kraken Spot:

- Query funds
- Query open orders and trades
- Modify orders
- Cancel/close orders

Permisos que no deben activarse:

- Withdrawals
- Margin
- Futures/derivatives

## Configuracion Principal

El routing crypto se controla desde `config/settings.yaml`:

```yaml
mode: paper
live_stage: stage_10

markets:
  crypto:
    exchange: kraken
    pairs: ["BTC/GBP", "ETH/GBP", "SOL/GBP"]
    quote_currency: GBP
    leverage_default: 1
    leverage_max: 1
    allow_short: false
    use_testnet: false
```

`GBP_USD_RATE` no se hardcodea. Solo hace falta definirlo si se usan pares con quote USD/USDT/USDC.
Para Kraken GBP Spot no hace falta.

## Manana Antes De Operar

1. Confirmar que el dinero ya aparece como GBP disponible en Kraken.
2. Ejecutar `python scripts/reset_runtime_state.py --confirm` si quieres arrancar con DB limpia.
3. Ejecutar `python main.py --scan` y confirmar que carga mercados Kraken.
4. Mantener `mode: paper` para una prueba completa sin dinero real.
5. Revisar Telegram con `/ready`: debe mostrar Kraken, GBP libre positivo y circuit breaker OK.
6. Revisar Telegram con `/golive` para el checklist corto.
7. Solo despues cambiar manualmente a `mode: semi_auto` para `stage_10`.
8. En `semi_auto`, aceptar cada trade con GO/SKIP. No usar `full_auto`.

## Fees Y PnL Neto

El sistema ahora estima fees de Kraken Spot en paper y guarda fees reales si Kraken los devuelve en live.

- Fee estimate por defecto: `markets.crypto.fees.taker_fee_pct: 0.40`.
- Cada alerta muestra `Fees est. ida/vuelta` y el `break-even` minimo para cubrir comisiones.
- Cada cierre muestra `Gross`, `Fees` y `Net`.
- `PnL total`, reportes y dashboard usan PnL neto despues de fees.
- En live, la validacion rechaza una orden si el saldo no cubre `notional + fee estimada`.

Con GBP 10, no aceptes una orden que use GBP 10.00 exactos; deja buffer para fees. Una prueba de GBP 9.50 es mas razonable.

## Salidas En Paper: ATR

En `mode: paper`, crypto usa niveles dinamicos por volatilidad:

- Stop = `max(2.5 * ATR14, piso por timeframe)`.
- Piso 1h: `3%`.
- Piso 4h: `4%`.
- TP1 = `1.5R`.
- TP2 = `2.5R`.

Esto reemplaza en paper al esquema fijo `SL 2% / TP1 3% / TP2 6%`.
Live sigue sin activarse hasta validar este perfil en paper.

## Estructura

```
config/          - Configuracion, settings, secrets y politicas
signals/         - Scanner, senales e indicadores tecnicos
execution/       - Ejecutores Kraken, Binance, OANDA e IBKR
risk/            - Gestion de riesgo y circuit breaker
notifications/   - Bot de Telegram y reportes
dashboard/       - Streamlit dashboard
data/            - Base de datos SQLite
main.py          - Entry point
```

## Modos De Operacion

| Modo | Descripcion |
|------|-------------|
| `paper` | Usa precios reales, simula ordenes localmente, no mueve dinero |
| `semi_auto` | Genera senales y requiere confirmacion GO/SKIP por Telegram |
| `full_auto` | Ejecuta sin confirmacion; no recomendado para Kraken en esta fase |
| `pause` | Congela ejecucion y evita nuevas operaciones |

## Telegram

Comandos clave para Kraken:

- `/status` - Portfolio, modo, exchange activo y pares.
- `/positions` - Posiciones del exchange activo + DB local reconciliada.
- `/ready` - Readiness, saldo Kraken, posiciones, ordenes y circuit breaker.
- `/golive` - Checklist corto antes de pasar a `semi_auto`.
- `/pause` - Pausa inmediata si algo no cuadra.
