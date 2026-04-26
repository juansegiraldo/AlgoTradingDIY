# Go-Live Checklist

## 1. Configuracion Y Credenciales

- [ ] Confirmar `config/settings.yaml`: `mode: paper` para la primera prueba y `markets.crypto.exchange: kraken`.
- [ ] Confirmar pares Kraken: `BTC/GBP`, `ETH/GBP`, `SOL/GBP`.
- [ ] Confirmar `markets.crypto.leverage_max: 1` y `allow_short: false`.
- [ ] Confirmar claves Kraken en variables de entorno o `config/secrets.yaml` bajo `kraken`.
- [ ] Resetear estado local si se quiere empezar limpio: `python scripts/reset_runtime_state.py --confirm`.
- [ ] Confirmar que la API key no tiene permisos de withdrawal, margin ni futures.
- [ ] Confirmar que Telegram responde a `/status`, `/pause`, `/resume`, `/positions`, `/ready` y `/golive`.

## 2. Salud De Infraestructura

- [ ] Ejecutar tests:

```bash
python -c "import os, pytest; os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'; raise SystemExit(pytest.main(['-q']))"
```

- [ ] Ejecutar `python main.py --scan` y confirmar que Kraken devuelve OHLCV para los pares GBP.
- [ ] Validar que `python main.py` levanta jobs programados sin excepciones.
- [ ] Confirmar que los logs y trades se guardan en SQLite (`data/trades.db`).
- [ ] Confirmar que el dashboard puede leer precios y posiciones.

## 3. Readiness Kraken

- [ ] Ejecutar `/ready` antes de la primera senal del dia.
- [ ] Ejecutar `/golive` y revisar el checklist corto.
- [ ] Confirmar que muestra exchange `kraken`.
- [ ] Confirmar GBP libre positivo en Kraken.
- [ ] Confirmar mercados cargados correctamente.
- [ ] Confirmar que la alerta muestra `Fees est. ida/vuelta` y no usa GBP 10.00 exactos.
- [ ] Confirmar circuit breaker en `OK`.
- [ ] Confirmar que no hay ordenes o posiciones inesperadas antes de operar.

## 4. Validaciones Funcionales En Paper

- [ ] Mantener `mode: paper` hasta que scanner, Telegram, dashboard y reportes funcionen.
- [ ] Forzar o esperar una senal y confirmar recepcion de GO/SKIP.
- [ ] Confirmar que la alerta paper muestra `Salida paper ATR` con SL/TP dinamicos.
- [ ] Confirmar ejecucion simulada y registro en DB.
- [ ] Confirmar que `/positions`, cierre y reporte muestran PnL neto, gross y fees.
- [ ] Confirmar que cierre manual, SL/TP y `/closeall` funcionan en paper.
- [ ] Revisar que PnL y balances se tratan como GBP para pares Kraken GBP.

## 5. Primer Switch A Live

- [ ] Cambiar a `mode: semi_auto` solo despues de un paper smoke limpio.
- [ ] Mantener `live_stage: stage_10`.
- [ ] Aceptar cada trade manualmente con GO/SKIP.
- [ ] No usar `full_auto`.
- [ ] No subir de etapa por PnL; subir solo si la operacion real fue estable.

## 6. Monitoreo Y Respuesta

- [ ] Si hay cualquier anomalia, ejecutar `/pause`.
- [ ] Comparar Kraken Pro vs SQLite vs dashboard.
- [ ] Revisar `/positions` antes de cualquier cierre manual.
- [ ] Si hace falta, cerrar con `/close` o `/closeall`.
- [ ] No ejecutar `/resume` hasta entender el fallo.

## 7. FX

- [ ] No definir `GBP_USD_RATE` para Kraken GBP Spot.
- [ ] Definir `GBP_USD_RATE` solo si se vuelven a usar pares USD, USDT o USDC.
