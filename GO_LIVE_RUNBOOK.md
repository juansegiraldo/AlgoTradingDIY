# Kraken Spot Runbook: `paper -> stage_10 -> stage_100 -> stage_1000`

## Reglas Fijas

- Crypto opera por Kraken Spot con pares GBP.
- No usar leverage, margin, futures, derivatives ni shorts.
- Operar real solamente en `semi_auto`.
- Confirmar siempre con `GO/SKIP` en Telegram.
- No subir de etapa por PnL; subir solo por estabilidad operativa.
- Si algo se ve raro, usar `/pause` antes de investigar.
- No activar `full_auto` en esta fase.

## Arranque Diario

1. Verificar que `config/settings.yaml` tiene `markets.crypto.exchange: kraken`.
2. Confirmar que los pares son `BTC/GBP`, `ETH/GBP` y `SOL/GBP`.
3. Si se quiere arrancar limpio, ejecutar `python scripts/reset_runtime_state.py --confirm`.
4. Mantener `mode: paper` hasta completar una prueba sin dinero real.
5. Ejecutar `python main.py --scan` para validar datos Kraken.
6. Arrancar el sistema y ejecutar `/ready`.
7. Ejecutar `/golive` para revisar el checklist corto desde Telegram.
8. Confirmar que `/ready` muestra Kraken, GBP libre positivo, mercados cargados y circuit breaker `OK`.
9. Solo si todo cuadra, cambiar a `mode: semi_auto` para la etapa live elegida.

## `paper`

- Objetivo: validar precios reales, senales, reportes, dashboard y Telegram sin mover dinero.
- Las ordenes se simulan localmente.
- Si falla el scanner, dashboard, Telegram o reporte matinal, no avanzar a `semi_auto`.

## `stage_10`

- Objetivo: validar plumbing real con riesgo minimo.
- Capital maximo operable: GBP 10.
- Maximo 1 posicion.
- Leverage maximo: 1.
- No forzar operaciones si Kraken rechaza por precision, minimo o balance.

## `stage_100`

- Subir solo si `stage_10` fue estable.
- Capital maximo operable: GBP 100.
- Maximo 2 posiciones.
- Mantener confirmacion manual total por Telegram.
- Revisar al cierre saldo, ordenes, posiciones, DB y logs.

## `stage_1000`

- Subir solo cuando `stage_10` y `stage_100` hayan sido limpios.
- Capital maximo operable: GBP 1000.
- Maximo 3 posiciones.
- Mantener readiness diario y pausa manual como control primario.
- No activar `full_auto`.

## Rollback Inmediato

1. Ejecutar `/pause`.
2. Revisar `/positions`.
3. Comparar Kraken vs SQLite vs dashboard.
4. Si hace falta, cerrar manualmente con `/close` o `/closeall`.
5. Verificar la cuenta directamente en Kraken Pro.
6. No ejecutar `/resume` hasta entender el fallo.

## Checklist Para Subir De Etapa

- Sin rechazos inesperados por precision o minimos.
- Sin desajustes entre Kraken y la base de datos.
- Sin fallos de cierre SL/TP/manual.
- Reporte matinal consistente varios dias seguidos.
- Logs sin errores criticos pendientes.
- `/ready` limpio antes de aceptar la primera senal del dia.
