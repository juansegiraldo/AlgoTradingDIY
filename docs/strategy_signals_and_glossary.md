# Regla 3 de 4 y Glosario Técnico

Este documento explica cómo funciona la regla de entrada `3 de 4` en este proyecto y aclara las siglas y términos técnicos usados por la estrategia.

## Resumen

La estrategia genera una señal de entrada cuando al menos `3 de 4` confirmaciones se alinean en la misma dirección.

Las 4 confirmaciones reales del sistema son:

1. `RSI`
2. `Cruce de EMA 9/21`
3. `Cruce de MACD`
4. `Volumen`

La `tendencia general` también se calcula, pero no forma parte del conteo de esas 4 confirmaciones. Se usa como contexto adicional.

## Dónde Se Configura

Los parámetros principales viven en `config/settings.yaml`.

Valores actuales:

- `rsi_period: 14`
- `ema_fast: 9`
- `ema_slow: 21`
- `ema_trend: 50`
- `ema_trend_long: 200`
- `macd_fast: 12`
- `macd_slow: 26`
- `macd_signal: 9`
- `volume_multiplier: 1.5`
- `volume_lookback: 20`
- `min_signals_for_entry: 3`

## Cómo Funciona La Regla 3 de 4

El sistema analiza cada activo y timeframe y evalúa cuatro confirmaciones.

Cada una puede:

- favorecer una entrada `long`
- favorecer una entrada `short`
- no activarse

Después suma cuántas confirmaciones apuntan a `long` y cuántas a `short`.

Regla de decisión:

- si `long >= 3`, genera una señal `long`
- si `short >= 3`, genera una señal `short`
- si ninguna dirección llega a 3, no hay entrada

El volumen no decide dirección por sí solo. Solo confirma la dirección que ya vaya ganando entre RSI, EMA y MACD.

## Las 4 Confirmaciones Reales del Sistema

### 1. RSI

El sistema usa `RSI 14`.

No basta con que el RSI esté alto o bajo. Lo que dispara la confirmación es el `cruce del nivel 50`.

Se interpreta así:

- `long`: el RSI cruza de abajo hacia arriba el nivel 50
- `short`: el RSI cruza de arriba hacia abajo el nivel 50

En otras palabras, el RSI se usa como una medida de cambio de impulso.

### 2. Cruce de EMA 9/21

El sistema usa dos medias móviles exponenciales:

- `EMA 9`
- `EMA 21`

La confirmación se activa cuando hay cruce entre ambas:

- `long`: la EMA 9 cruza por encima de la EMA 21
- `short`: la EMA 9 cruza por debajo de la EMA 21

Esto se usa como confirmación de dirección a corto plazo.

### 3. Cruce de MACD

El sistema usa:

- `MACD fast: 12`
- `MACD slow: 26`
- `MACD signal: 9`

La confirmación se activa cuando la línea MACD cruza su línea de señal:

- `long`: la línea MACD cruza por encima de la línea signal
- `short`: la línea MACD cruza por debajo de la línea signal

Esto mide si el impulso del movimiento está acelerando o perdiendo fuerza en una dirección concreta.

### 4. Volumen

El volumen se evalúa comparando:

- el `volumen actual`
- contra el `promedio de volumen` de las últimas `20` velas

La confirmación se activa si:

- `volumen actual > 1.5 x promedio de 20 velas`

Importante:

- el volumen no es `long` ni `short` por sí mismo
- el volumen solo confirma que el movimiento actual tiene participación suficiente
- si RSI, EMA y MACD están empatados, el volumen no rompe el empate

## Tendencia General

Aunque no cuenta dentro de las 4 confirmaciones, el sistema también calcula la tendencia general usando:

- `EMA 50`
- `EMA 200`

Se interpreta así:

- `bullish`: `precio > EMA 50 > EMA 200`
- `bearish`: `precio < EMA 50 < EMA 200`
- `mixed`: cualquier otra combinación

Esto sirve como contexto de mercado:

- ayuda a entender si la señal está a favor o en contra de la estructura general
- también se usa para tomar el precio actual de referencia al construir la operación

## Cómo Se Vería un Caso Real

Ejemplo de señal `long` válida:

- RSI cruza por encima de 50
- EMA 9 cruza por encima de EMA 21
- MACD cruza por encima de su signal
- volumen actual está por encima de `1.5 x` su promedio

Resultado:

- hay `4 de 4`
- la dirección es `long`
- la señal se clasifica como `strong`

Ejemplo de señal `short` válida:

- RSI cruza por debajo de 50
- EMA 9 cruza por debajo de EMA 21
- MACD no se activa
- volumen sí se activa

Resultado:

- hay `3 de 4`
- la dirección es `short`
- la señal se clasifica como `moderate`

Ejemplo sin entrada:

- RSI da `long`
- EMA da `short`
- MACD no se activa
- volumen se activa

Resultado:

- no hay una dirección dominante con al menos 3 confirmaciones
- no se genera señal

## Fuerza de la Señal

El sistema clasifica la señal así:

- `strong`: 4 confirmaciones
- `moderate`: 3 confirmaciones
- `weak`: menos de 3, aunque en ese caso no se genera entrada

## Precio de Entrada, Stop Loss y Take Profit

Cuando la señal ya fue aprobada, el sistema construye la operación usando el precio actual y reglas fijas de gestión de posición.

Valores actuales:

- `stop_loss_pct: 2.0`
- `take_profit_1_pct: 3.0`
- `take_profit_2_pct: 6.0`

Para una entrada `long`:

- `Stop Loss = precio x (1 - 0.02)`
- `TP1 = precio x (1 + 0.03)`
- `TP2 = precio x (1 + 0.06)`

Para una entrada `short`:

- `Stop Loss = precio x (1 + 0.02)`
- `TP1 = precio x (1 - 0.03)`
- `TP2 = precio x (1 - 0.06)`

## Glosario

### ADX

`Average Directional Index`.

Indicador que mide la fuerza de una tendencia. En este proyecto no forma parte de la regla `3 de 4`.

### Activo

Instrumento que se analiza o se opera, como `BTC/USDT`, `EUR_USD` o `TQQQ`.

### Análisis

Resultado de calcular los indicadores técnicos sobre un conjunto de velas.

### Bearish

Contexto bajista. El mercado o la estructura favorece caídas.

### Bullish

Contexto alcista. El mercado o la estructura favorece subidas.

### Confirmación

Condición técnica que debe cumplirse para respaldar una posible entrada.

### Cruce

Momento en el que una línea pasa de estar por debajo a por encima de otra, o al revés.

### EMA

`Exponential Moving Average`.

Media móvil exponencial. Da más peso a los datos recientes que una media móvil simple.

### EMA 9/21

Pareja de medias móviles exponenciales usada en el sistema para detectar cambios de dirección de corto plazo.

### EMA 50/200

Pareja de medias móviles exponenciales usada para medir la tendencia general del mercado.

### Histogram

En MACD, diferencia entre la línea MACD y la línea signal. Ayuda a ver si la separación entre ambas se expande o se contrae.

### Indicador Técnico

Cálculo matemático aplicado al precio o al volumen para extraer señales o contexto.

### Long

Operación que busca ganar si el precio sube.

### MACD

`Moving Average Convergence Divergence`.

Indicador que compara dos medias para medir impulso y cambios de dirección.

### Momentum

Ritmo o impulso del movimiento del precio. En esta estrategia se refleja sobre todo en RSI y MACD.

### Mixed

Estado de tendencia no clara. No hay alineación limpia entre precio, EMA 50 y EMA 200.

### OHLCV

Conjunto de datos de una vela:

- `Open`
- `High`
- `Low`
- `Close`
- `Volume`

### Precio

En este contexto, normalmente se refiere al `close` más reciente usado para construir la entrada.

### RSI

`Relative Strength Index`.

Indicador de impulso. En este sistema se usa el cruce del nivel 50 para decidir si el momentum favorece `long` o `short`.

### Senal

Resultado final del análisis cuando el sistema encuentra suficientes confirmaciones para justificar una entrada.

### Senal de Entrada

Operación candidata que ya cumple las condiciones mínimas para pasar a validación de riesgo y eventual ejecución.

### Short

Operación que busca ganar si el precio baja.

### Signal Line

Línea de señal del MACD. Se compara contra la línea principal de MACD para detectar cruces.

### Stop Loss

Nivel de salida que limita la pérdida si el mercado va en contra.

### Take Profit

Nivel de salida parcial o total para asegurar ganancia cuando el precio llega al objetivo.

### Timeframe

Marco temporal de las velas analizadas, como `1h`, `4h`, `H4` o `D`.

### Tendencia

Dirección general del mercado en una ventana más amplia. En este proyecto se mide con la relación entre precio, EMA 50 y EMA 200.

### Triggered

Estado que indica que una condición sí se activó en la vela o revisión actual.

### Vela

Unidad de información de precio en un periodo concreto. Contiene apertura, máximo, mínimo, cierre y volumen.

### Volumen

Cantidad negociada durante una vela o periodo. En esta estrategia se usa como confirmación extra del movimiento.

## Resumen Ejecutivo

La estrategia de este proyecto no trabaja con una idea abstracta de "tendencia, fuerza, momento y volumen" como cuatro bloques formales.

Trabaja con cuatro confirmaciones concretas:

- `RSI`
- `EMA 9/21`
- `MACD`
- `Volumen`

Y calcula aparte una tendencia estructural con:

- `precio`
- `EMA 50`
- `EMA 200`

En términos prácticos:

- `3 de 4` = señal válida
- `4 de 4` = señal más fuerte
- menos de `3 de 4` = no se entra
