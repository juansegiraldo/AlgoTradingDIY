# 10X Trading System

Crypto | Forex | ETFs Apalancados

**Capital Inicial:** GBP 1,000 | **Horizonte:** 1-3 Meses | **Modo:** Pasivo con Alertas

> **ADVERTENCIA:** Este sistema es un proyecto educativo. No constituye asesoramiento financiero.
> Las estrategias con apalancamiento pueden resultar en la perdida total del capital invertido.

## Quick Start

```bash
# 1. Crear entorno virtual
python -m venv venv
source venv/bin/activate  # Linux/Mac
venv\Scripts\activate     # Windows

# 2. Instalar dependencias
pip install -r requirements.txt

# 3. Configurar API keys
# Editar config/secrets.yaml con tus claves

# 4. Ejecutar en modo paper trading
python main.py
```

## Estructura

```
config/          - Configuracion (settings, secrets, risk policies)
signals/         - Motor de senales e indicadores tecnicos
execution/       - Ejecutores por broker (Binance, OANDA, IBKR)
risk/            - Gestion de riesgo y circuit breaker
notifications/   - Bot de Telegram y reportes
dashboard/       - Streamlit dashboard
data/            - Base de datos SQLite
main.py          - Entry point
```

## Modos de Operacion

| Modo | Descripcion |
|------|-------------|
| `paper` | Simula trades sin dinero real (obligatorio las primeras 2 semanas) |
| `semi_auto` | Genera senales, el usuario confirma via Telegram |
| `full_auto` | Ejecuta sin confirmacion (con politicas de riesgo activas) |
| `pause` | Congela todo |
