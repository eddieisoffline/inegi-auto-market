# Pronóstico de ventas con backtesting

Un pronóstico a 1–6 meses que dice cuánto se equivoca. Código:
[`src/inegi_market/forecast.py`](../src/inegi_market/forecast.py).

```bash
python -m inegi_market.cli forecast                     # backtest, métricas y próximos 6 meses
python -m inegi_market.cli forecast --sin-ajuste-2020   # para comparar el tratamiento de 2020
```

## Series y modelos

- **Series:** ventas nacionales mensuales y las de las 5 marcas con más ventas en los
  últimos 12 meses (hoy: Nissan, General Motors, Volkswagen, Toyota y KIA), de la foto
  curated más reciente. Cada serie de marca empieza en su primer mes con ventas
  (KIA llega en 2015-07).
- **Modelos:**

| Modelo | Qué es |
| --- | --- |
| `ingenuo_estacional` | **Línea base obligatoria:** el mismo mes del año anterior |
| `ets` | ETS aditivo con tendencia amortiguada sobre log(ventas) |
| `sarima` | SARIMA(1,1,1)(0,1,1)12 sobre log(ventas), órdenes fijos |
| `sarimax_fx` | El mismo SARIMA con log(tipo de cambio FIX) **rezagado 6 meses**: para pronosticar hasta 6 meses usa tipos de cambio que ya se conocían, así no se pronostica el tipo de cambio ni se filtra el futuro |

Los pronósticos en logaritmo se regresan con la exponencial (son la mediana, no la
media). Los intervalos son de 80 %.

## Validación

- **Origen móvil.** Para cada mes de origen, cada modelo se ajusta solo con datos hasta
  ese mes y pronostica 1 a 6 meses. Se evalúan los meses objetivo desde 2023-01 (después
  de la pandemia y de la escasez de chips) hasta el último publicado: 45 meses, con 51
  orígenes. Todos los modelos se comparan sobre los mismos meses.
- **Métricas** por horizonte (pronósticos a 1–3 y a 1–6 meses): MAE, RMSE, MASE y MAPE
  (este solo si no hay ceros en los datos reales). MASE divide cada error absoluto entre
  el error medio del ingenuo estacional dentro de la muestra de entrenamiento: menos de 1
  significa mejor que repetir el año anterior.
- **Ganador:** el modelo con menor MASE en cada serie y horizonte. La mejora se reporta
  contra la línea base; un modelo que no le gana a la base no se reporta como ganador.

## Tratamiento de 2020

De marzo a junio de 2020 las ventas cayeron entre 26 % y 64 % contra 2019 (confinamiento):
son valores atípicos, no un cambio de nivel. De julio de 2020 a 2022 las ventas siguieron
entre 20 % y 34 % abajo (pandemia y escasez de chips): eso sí es nivel y se deja.

- **Para entrenar,** marzo a junio de 2020 se reemplazan por una trayectoria sin
  confinamiento: el mismo mes de 2019 por la razón 2020/2019 interpolada en línea recta
  entre febrero (1.00) y julio (0.69) de 2020.
- **No se ajusta:** la evaluación (siempre con datos reales) ni la línea base.
- **La escala de MASE** excluye las diferencias que tocan 2020.

## Resultados (datos hasta 2026-09, evaluación 2023-01 a 2026-09)

MASE de cada modelo; menor es mejor. **Todos los ganadores le ganan a la línea base.**

| Serie | Horizonte | Ingenuo estacional | ETS | SARIMA | SARIMAX con tipo de cambio | Ganador | Mejora sobre la base | MAPE del ganador |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Nacional | 1–3 | 1.087 | 0.621 | **0.527** | 0.530 | SARIMA | 52 % | 4.5 % |
| Nacional | 1–6 | 1.093 | 0.808 | **0.648** | 0.656 | SARIMA | 41 % | 5.6 % |
| Nissan | 1–3 | 0.883 | 0.630 | **0.624** | 0.628 | SARIMA | 29 % | 9.3 % |
| Nissan | 1–6 | 0.885 | **0.763** | 0.780 | 0.789 | ETS | 14 % | 11.3 % |
| General Motors | 1–3 | 0.504 | 0.507 | 0.436 | **0.433** | SARIMAX | 14 % | 7.1 % |
| General Motors | 1–6 | 0.503 | 0.496 | 0.479 | **0.471** | SARIMAX | 6 % | 7.8 % |
| Volkswagen | 1–3 | 0.925 | **0.543** | 0.639 | 0.672 | ETS | 41 % | 8.0 % |
| Volkswagen | 1–6 | 0.928 | **0.658** | 0.823 | 0.870 | ETS | 29 % | 9.7 % |
| Toyota | 1–3 | 1.239 | 1.085 | 0.946 | **0.935** | SARIMAX | 24 % | 9.8 % |
| Toyota | 1–6 | 1.242 | 1.233 | 0.971 | **0.950** | SARIMAX | 24 % | 10.0 % |
| KIA | 1–3 | 0.516 | **0.374** | 0.388 | 0.376 | ETS | 28 % | 4.5 % |
| KIA | 1–6 | 0.512 | 0.407 | 0.432 | **0.406** | SARIMAX | 21 % | 5.0 % |

Lectura:

- **Nacional:** SARIMA reduce el error de la línea base a la mitad a 1–3 meses (MASE
  0.527 contra 1.087; error medio de 4.5 %).
- **No hay un modelo que gane siempre:** SARIMA gana en la serie nacional, ETS en
  Volkswagen y SARIMAX en General Motors y Toyota. En General Motors, ETS no le gana a la
  línea base.
- **El tipo de cambio aporta poco y no siempre:** con el rezago de 6 meses, SARIMAX mejora
  a SARIMA en General Motors, Toyota y KIA (2 % a 6 % de MASE) y lo empeora en la serie
  nacional, Nissan y Volkswagen (1 % a 6 %).
- **Pronóstico nacional** (SARIMA, intervalo de 80 %): 134,650 en octubre de 2026
  (125,153 a 144,867) y 164,473 en diciembre (148,527 a 182,132).

### Sensibilidad al tratamiento de 2020

MASE del ganador con el ajuste de 2020 (por defecto) y sin él:

| Serie | 1–3 meses | 1–6 meses |
| --- | --- | --- |
| Nacional | 0.527 → 0.549 | 0.648 → 0.639 |
| Nissan | 0.624 → 0.624 | 0.763 → 0.726 |
| General Motors | 0.433 → 0.451 | 0.471 → 0.469 |
| Volkswagen | 0.543 → 0.513 | 0.658 → 0.607 |
| Toyota | 0.935 → 0.968 | 0.950 → 0.988 |
| KIA | 0.374 → **0.516 (gana la línea base)** | 0.406 → **0.512 (gana la línea base)** |

El ajuste no es una mejora uniforme: ayuda en 6 de 12 casos y empeora en 5. Se deja por
defecto porque evita el peor caso: sin él, ningún modelo le gana a la línea base en KIA.

## Salidas

| Ruta | Contenido |
| --- | --- |
| `curated/forecast_results/` | métricas por serie, modelo y horizonte, con ganador y mejora sobre la base |
| `curated/forecast_predictions/` | próximos 6 meses por serie y modelo, con intervalo de 80 % (la línea base no tiene intervalo) |
| `curated/forecast_backtest/` | cada pronóstico del backtest: origen, horizonte, mes objetivo, real y pronóstico |
| `reports/forecast/datos_hasta=AAAA-MM.json` | resumen: ganador, MASE, MAPE y mejora por serie y horizonte |

En el warehouse: `fct_forecast_results`, `fct_forecast_predictions`,
`fct_forecast_backtest` y las vistas `mart_pronostico` (real y pronóstico juntos) y
`mart_pronostico_metricas`.

## Limitaciones

- **El backtest usa la foto más reciente,** no la que existía en cada origen (solo hay
  dos fotos guardadas). Las revisiones observadas son pequeñas (−1 unidad en la historia
  nacional), pero en rigor el error real en tiempo real podría ser algo mayor.
- **Órdenes fijos:** SARIMA y ETS no se eligen automáticamente en cada origen.
- **Convergencia:** 20 de unos 900 ajustes del backtest no convergieron del todo (se usan
  igual y el reporte los cuenta).
- **Reproducibilidad:** los modelos son deterministas y la semilla está fija (20261008).
  Una ejecución tarda alrededor de 75 s, con una serie por proceso.
