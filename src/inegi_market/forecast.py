"""Pronóstico de ventas a 1-6 meses, evaluado contra el ingenuo estacional.

Series: ventas nacionales mensuales y las de las 5 marcas con más ventas en los últimos 12
meses (foto curated más reciente). Modelos:

    ingenuo_estacional  línea base obligatoria: el mismo mes del año anterior
    ets                 ETS aditivo con tendencia amortiguada sobre log(ventas)
    sarima              SARIMA(1,1,1)(0,1,1)12 sobre log(ventas)
    sarimax_fx          el mismo SARIMA con log(tipo de cambio FIX) rezagado 6 meses como
                        variable externa: con ese rezago, el dato que usa cada mes ya era
                        conocido al pronosticar (no se pronostica el tipo de cambio)

Validación de origen móvil: en cada origen se ajusta cada modelo solo con datos hasta ese
mes y se pronostican 1 a 6 meses. Se evalúan los meses objetivo desde `eval_start`
(2023-01: después de la pandemia y de la escasez de chips). Métricas por horizonte
(pronósticos a 1-3 y a 1-6 meses): MAE, RMSE, MASE y MAPE (este último solo si no hay
ceros en los datos reales).

2020 se trata de forma explícita: para entrenar los modelos, marzo a junio de 2020 se
reemplazan por una trayectoria sin confinamiento (el mismo mes de 2019 por la razón
2020/2019 interpolada entre febrero y julio de 2020). La evaluación siempre usa los datos
reales, la línea base no se ajusta y la escala de MASE excluye las diferencias que tocan
2020. Con `adjust_2020=False` se entrena con la serie sin ajustar, para comparar.

    curated/forecast_results/part-0.parquet       métricas por serie, modelo y horizonte
    curated/forecast_predictions/part-0.parquet   próximos 6 meses, con intervalo de 80 %
    curated/forecast_backtest/part-0.parquet      cada pronóstico del backtest
    reports/forecast/datos_hasta=AAAA-MM.json     resumen: quién gana y por cuánto
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import warnings
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timezone
from itertools import repeat

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .storage import Storage

log = logging.getLogger(__name__)

SEASON = 12
MAX_H = 6
FX_LAG = 6
SEED = 20261008  # los modelos son deterministas; la semilla queda fija por si alguno simula
INTERVAL = 0.80
NATIONAL = "nacional"
SHOCK_2020 = [pd.Timestamp(f"2020-{m:02d}-01") for m in (3, 4, 5, 6)]

RESULTS_PATH = "curated/forecast_results/part-0.parquet"
PREDICTIONS_PATH = "curated/forecast_predictions/part-0.parquet"
BACKTEST_PATH = "curated/forecast_backtest/part-0.parquet"
REPORT_PREFIX = "reports/forecast"


class ForecastError(ValueError):
    pass


@dataclass(frozen=True)
class ForecastConfig:
    eval_start: date = date(2023, 1, 1)  # primer mes objetivo que se evalúa
    horizons: tuple[int, ...] = (3, 6)
    top_brands: int = 5
    adjust_2020: bool = True
    workers: int = 0  # procesos en paralelo, uno por serie (0: según los núcleos; 1: en secuencia)


# --- datos ----------------------------------------------------------------------------------------


def _latest(storage: Storage, prefix: str) -> list[str]:
    files = storage.list(prefix)
    found = (re.search(r"publication_date=([\d-]+)/", f) for f in files)
    dates = sorted({m.group(1) for m in found if m})
    if not dates:
        raise ForecastError(f"no hay curated en {prefix}: corre 'curate'")
    return [f for f in files if f"publication_date={dates[-1]}/" in f]


def _read(storage: Storage, files: list[str], columns: list[str]) -> pd.DataFrame:
    return pd.concat([pd.read_parquet(io.BytesIO(storage.read_bytes(f)), columns=columns)
                      for f in files], ignore_index=True)


def _monthly(values: pd.Series) -> pd.Series:
    values.index = pd.DatetimeIndex(pd.to_datetime(values.index), freq="MS")
    return values.astype("float64")


def load_sales(storage: Storage, top_brands: int) -> dict[str, pd.Series]:
    """Serie nacional y de las marcas con más ventas en los últimos 12 meses."""
    df = _read(storage, _latest(storage, "curated/venta/"), ["periodo", "marca", "unidades"])
    national = df.groupby("periodo")["unidades"].sum().sort_index()
    full = pd.date_range(pd.Timestamp(national.index.min()), pd.Timestamp(national.index.max()),
                         freq="MS")
    if len(full) != len(national):
        raise ForecastError("la serie nacional tiene meses faltantes")
    series = {NATIONAL: _monthly(national)}
    last_year = df[pd.to_datetime(df["periodo"]) > full[-1] - pd.DateOffset(months=12)]
    leaders = last_year.groupby("marca")["unidades"].sum().nlargest(top_brands).index
    for brand in leaders:
        values = df[df["marca"] == brand].groupby("periodo")["unidades"].sum()
        values.index = pd.to_datetime(values.index)
        values = values.reindex(full, fill_value=0)
        # La serie empieza en el primer mes con ventas (p. ej., KIA llega en 2015-07).
        series[brand] = _monthly(values[values.gt(0).idxmax():])
    return series


def load_fx(storage: Storage) -> pd.Series:
    path = "curated/tipo_cambio_mensual/part-0.parquet"
    if not storage.exists(path):
        raise ForecastError("no hay tipo de cambio en curated: corre 'fx'")
    df = pd.read_parquet(io.BytesIO(storage.read_bytes(path)),
                         columns=["periodo", "tipo_cambio_promedio"])
    fx = df.set_index("periodo")["tipo_cambio_promedio"].astype("float64").sort_index()
    return _monthly(fx)


def adjust_2020(y: pd.Series) -> pd.Series:
    """Marzo a junio de 2020 con la trayectoria sin confinamiento (solo para entrenar)."""
    february, july = pd.Timestamp("2020-02-01"), pd.Timestamp("2020-07-01")
    needed = [february, july, *SHOCK_2020]
    needed += [d - pd.DateOffset(years=1) for d in needed]
    if not all(d in y.index for d in needed) or (y.reindex(needed) <= 0).any():
        return y  # la serie no cubre 2019-2020 completo (p. ej., un origen anterior)
    start = y[february] / y[february - pd.DateOffset(years=1)]
    end = y[july] / y[july - pd.DateOffset(years=1)]
    adjusted = y.copy()
    for step, month in enumerate(SHOCK_2020, start=1):
        ratio = start + (end - start) * step / 5  # febrero = 0, julio = 5
        adjusted[month] = y[month - pd.DateOffset(years=1)] * ratio
    return adjusted


def mase_scale(y: pd.Series) -> float:
    """MAE del ingenuo estacional dentro de la muestra, sin las diferencias que tocan 2020."""
    diff = (y - y.shift(SEASON)).dropna()
    previous_year = (diff.index - pd.DateOffset(years=1)).year
    touches_2020 = (diff.index.year == 2020) | (previous_year == 2020)
    scale = float(diff[~touches_2020].abs().mean())
    if not scale > 0:
        raise ForecastError("la escala de MASE es cero o no se puede calcular")
    return scale


# --- modelos ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Forecast:
    mean: np.ndarray
    lower: np.ndarray | None = None
    upper: np.ndarray | None = None
    converged: bool = True


def _future(y: pd.Series, steps: int) -> pd.DatetimeIndex:
    return pd.date_range(y.index[-1] + pd.DateOffset(months=1), periods=steps, freq="MS")


def seasonal_naive(y: pd.Series, steps: int, fx: pd.Series | None = None) -> Forecast:
    if steps > SEASON:
        raise ForecastError("el ingenuo estacional solo llega a 12 meses")
    return Forecast(y.iloc[-SEASON:].to_numpy()[:steps].copy())


def _log(y: pd.Series) -> pd.Series:
    if (y <= 0).any():
        raise ForecastError("los modelos en logaritmo necesitan ventas positivas")
    return np.log(y)


def ets(y: pd.Series, steps: int, fx: pd.Series | None = None) -> Forecast:
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel

    model = ETSModel(_log(y), error="add", trend="add", damped_trend=True, seasonal="add",
                     seasonal_periods=SEASON)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = model.fit(disp=False, maxiter=500)
        frame = fit.get_prediction(start=len(y), end=len(y) + steps - 1).summary_frame(
            alpha=1 - INTERVAL)
    return Forecast(np.exp(frame["mean"].to_numpy()), np.exp(frame["pi_lower"].to_numpy()),
                    np.exp(frame["pi_upper"].to_numpy()),
                    bool(fit.mle_retvals.get("converged", True)))


def _sarimax(y: pd.Series, steps: int, exog=None, exog_future=None) -> Forecast:
    from statsmodels.tsa.statespace.sarimax import SARIMAX

    model = SARIMAX(_log(y), exog=exog, order=(1, 1, 1), seasonal_order=(0, 1, 1, SEASON),
                    enforce_stationarity=False, enforce_invertibility=False)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fit = model.fit(disp=False, maxiter=200)
        prediction = fit.get_forecast(steps=steps, exog=exog_future)
    bounds = prediction.conf_int(alpha=1 - INTERVAL).to_numpy()
    return Forecast(np.exp(prediction.predicted_mean.to_numpy()), np.exp(bounds[:, 0]),
                    np.exp(bounds[:, 1]), bool(fit.mle_retvals.get("converged", True)))


def sarima(y: pd.Series, steps: int, fx: pd.Series | None = None) -> Forecast:
    return _sarimax(y, steps)


def sarimax_fx(y: pd.Series, steps: int, fx: pd.Series | None = None) -> Forecast:
    """log(tipo de cambio) del mes t-6 como variable externa del mes t."""
    if fx is None:
        raise ForecastError("sarimax_fx necesita el tipo de cambio")
    if fx.index[-1] < y.index[-1]:
        raise ForecastError("el tipo de cambio no llega al origen del pronóstico")
    if steps > FX_LAG:
        raise ForecastError(f"sarimax_fx pronostica a lo más {FX_LAG} meses")
    log_fx = np.log(fx)
    train = log_fx.reindex(y.index - pd.DateOffset(months=FX_LAG))
    train.index = y.index
    keep = train.notna()  # los primeros 6 meses de la serie no tienen tipo de cambio rezagado
    # El mes t usa el tipo de cambio de t-6: para t <= origen + 6 ya es conocido.
    future = log_fx.reindex(_future(y, steps) - pd.DateOffset(months=FX_LAG))
    if future.isna().any():
        raise ForecastError("falta tipo de cambio para los meses que usa el pronóstico")
    return _sarimax(y[keep], steps, train[keep].to_numpy().reshape(-1, 1),
                    future.to_numpy().reshape(-1, 1))


MODELS: dict[str, Callable[..., Forecast]] = {
    "ingenuo_estacional": seasonal_naive,
    "ets": ets,
    "sarima": sarima,
    "sarimax_fx": sarimax_fx,
}
BASELINE = "ingenuo_estacional"


# --- backtest y métricas ----------------------------------------------------------------------


def origins(y: pd.Series, eval_start: date, max_h: int = MAX_H) -> list[pd.Timestamp]:
    """Orígenes cuyos pronósticos a 1-max_h caen en meses evaluables (>= eval_start)."""
    first = pd.Timestamp(eval_start) - pd.DateOffset(months=max_h)
    return [o for o in y.index if first <= o < y.index[-1]]


def backtest(
    serie: str,
    y: pd.Series,
    fx: pd.Series | None,
    config: ForecastConfig,
    models: dict[str, Callable[..., Forecast]] = MODELS,
) -> tuple[pd.DataFrame, int]:
    """Cada pronóstico del origen móvil; solo usa datos hasta el origen. Devuelve también
    cuántos ajustes no convergieron."""
    rows, not_converged = [], 0
    max_h = max(config.horizons)
    for origin in origins(y, config.eval_start, max_h):
        raw = y[:origin]
        train = adjust_2020(raw) if config.adjust_2020 else raw
        scale = mase_scale(raw)
        known_fx = fx[:origin] if fx is not None else None
        for name, model in models.items():
            forecast = model(raw if name == BASELINE else train, max_h, known_fx)
            not_converged += not forecast.converged
            for h in range(1, max_h + 1):
                target = origin + pd.DateOffset(months=h)
                if target < pd.Timestamp(config.eval_start) or target not in y.index:
                    continue
                rows.append({"serie": serie, "modelo": name, "origen": origin.date(), "h": h,
                             "objetivo": target.date(), "real": float(y[target]),
                             "pronostico": float(forecast.mean[h - 1]), "escala_mase": scale})
    return pd.DataFrame(rows), not_converged


def metrics(errors: pd.DataFrame) -> dict[str, float | None]:
    e = errors["real"] - errors["pronostico"]
    has_zero = (errors["real"] == 0).any()
    return {
        "mae": float(e.abs().mean()),
        "rmse": float(np.sqrt((e ** 2).mean())),
        "mase": float((e.abs() / errors["escala_mase"]).mean()),
        "mape": None if has_zero else float((e.abs() / errors["real"].abs()).mean()),
    }


def summarize(detail: pd.DataFrame, config: ForecastConfig) -> pd.DataFrame:
    rows = []
    for (serie, modelo), group in detail.groupby(["serie", "modelo"], sort=False):
        for horizon in config.horizons:
            part = group[group["h"] <= horizon]
            rows.append({"serie": serie, "modelo": modelo, "horizonte": horizon,
                         "n_pronosticos": len(part), **metrics(part),
                         "primer_objetivo": part["objetivo"].min(),
                         "ultimo_objetivo": part["objetivo"].max()})
    out = pd.DataFrame(rows)
    base = out[out["modelo"] == BASELINE].set_index(["serie", "horizonte"])["mase"]
    out["mejora_mase_vs_base"] = [1 - m / base[(s, h)] for s, h, m in
                                  zip(out["serie"], out["horizonte"], out["mase"], strict=True)]
    best = out.groupby(["serie", "horizonte"])["mase"].transform("min")
    out["es_ganador"] = out["mase"] == best
    return out


# --- ejecución --------------------------------------------------------------------------------


def final_forecasts(serie: str, y: pd.Series, fx: pd.Series | None, config: ForecastConfig,
                    winners: set[str], models=MODELS) -> pd.DataFrame:
    train = adjust_2020(y) if config.adjust_2020 else y
    future = _future(y, MAX_H)
    rows = []
    for name, model in models.items():
        forecast = model(y if name == BASELINE else train, MAX_H, fx)
        for i, month in enumerate(future):
            rows.append({
                "serie": serie, "modelo": name, "periodo": month.date(),
                "pronostico": float(forecast.mean[i]),
                "limite_inferior_80": None if forecast.lower is None else float(forecast.lower[i]),
                "limite_superior_80": None if forecast.upper is None else float(forecast.upper[i]),
                "es_ganador": name in winners,
            })
    return pd.DataFrame(rows)


RESULTS_SCHEMA = pa.schema([
    ("serie", pa.string()), ("modelo", pa.string()), ("horizonte", pa.int64()),
    ("n_pronosticos", pa.int64()), ("mae", pa.float64()), ("rmse", pa.float64()),
    ("mase", pa.float64()), ("mape", pa.float64()), ("mejora_mase_vs_base", pa.float64()),
    ("es_ganador", pa.bool_()), ("primer_objetivo", pa.date32()),
    ("ultimo_objetivo", pa.date32()),
    ("ajuste_2020", pa.bool_()), ("datos_hasta", pa.date32()), ("fecha_ejecucion", pa.date32()),
    ("semilla", pa.int64()),
])
PREDICTIONS_SCHEMA = pa.schema([
    ("serie", pa.string()), ("modelo", pa.string()), ("periodo", pa.date32()),
    ("pronostico", pa.float64()), ("limite_inferior_80", pa.float64()),
    ("limite_superior_80", pa.float64()), ("es_ganador", pa.bool_()),
    ("datos_hasta", pa.date32()), ("fecha_ejecucion", pa.date32()),
])
BACKTEST_SCHEMA = pa.schema([
    ("serie", pa.string()), ("modelo", pa.string()), ("origen", pa.date32()), ("h", pa.int64()),
    ("objetivo", pa.date32()), ("real", pa.float64()), ("pronostico", pa.float64()),
    ("escala_mase", pa.float64()), ("datos_hasta", pa.date32()),
])


def _parquet(df: pd.DataFrame, schema: pa.Schema) -> bytes:
    records = [{k: (None if isinstance(v, float) and np.isnan(v) else v) for k, v in r.items()}
               for r in df[list(schema.names)].to_dict("records")]
    buffer = io.BytesIO()
    pq.write_table(pa.Table.from_pylist(records, schema=schema), buffer)
    return buffer.getvalue()


def run_series(
    serie: str, y: pd.Series, fx: pd.Series, config: ForecastConfig
) -> tuple[pd.DataFrame, int, pd.DataFrame, pd.DataFrame]:
    """Backtest, métricas y pronóstico final de una serie (se puede correr en otro proceso)."""
    try:
        detail, failures = backtest(serie, y, fx, config)
    except ForecastError as exc:
        raise ForecastError(f"{serie}: {exc}") from None
    summary = summarize(detail, config)
    winners = set(summary[(summary["horizonte"] == max(config.horizons))
                          & summary["es_ganador"]]["modelo"])
    return detail, failures, summary, final_forecasts(serie, y, fx, config, winners)


def run_forecast(
    storage: Storage,
    config: ForecastConfig | None = None,
    today: Callable[[], date] = lambda: datetime.now(timezone.utc).date(),
    write: bool = True,
) -> dict:
    """Backtest, métricas y pronóstico final de todas las series. Devuelve el resumen."""
    config = config or ForecastConfig()
    np.random.seed(SEED)
    series = load_sales(storage, config.top_brands)
    fx = load_fx(storage)
    last = series[NATIONAL].index[-1]
    if fx.index[-1] < last:
        raise ForecastError("el tipo de cambio no llega al último mes de ventas: corre 'fx'")
    fx = fx[:last]  # el pronóstico final no usa meses posteriores a las ventas

    workers = config.workers or min(len(series), os.cpu_count() or 1)
    names, values = list(series), list(series.values())
    if workers > 1:
        with ProcessPoolExecutor(max_workers=workers) as pool:
            outputs = list(pool.map(run_series, names, values, repeat(fx), repeat(config)))
    else:
        outputs = [run_series(n, v, fx, config) for n, v in zip(names, values, strict=True)]
    details = [o[0] for o in outputs]
    not_converged = sum(o[1] for o in outputs)
    results = [o[2] for o in outputs]
    predictions = [o[3] for o in outputs]
    for summary in results:
        for row in summary[summary["es_ganador"]].itertuples():
            log.info("%s, 1-%d meses: gana %s (MASE %.3f, %+.1f %% contra la línea base)",
                     row.serie, row.horizonte, row.modelo, row.mase,
                     100 * row.mejora_mase_vs_base)

    stamp = {"datos_hasta": last.date(), "fecha_ejecucion": today()}
    results_df = pd.concat(results, ignore_index=True).assign(
        ajuste_2020=config.adjust_2020, semilla=SEED, **stamp)
    predictions_df = pd.concat(predictions, ignore_index=True).assign(**stamp)
    detail_df = pd.concat(details, ignore_index=True).assign(datos_hasta=last.date())
    report = {
        "datos_hasta": str(last.date())[:7],
        "eval_start": str(config.eval_start)[:7],
        "ajuste_2020": config.adjust_2020,
        "ajustes_sin_convergencia": not_converged,
        "series": {
            serie: {
                f"1-{h}": {
                    "ganador": g.loc[g["es_ganador"], "modelo"].tolist(),
                    "mase": {r.modelo: round(r.mase, 4) for r in g.itertuples()},
                    "mejora_vs_base": {r.modelo: round(r.mejora_mase_vs_base, 4)
                                       for r in g.itertuples()},
                    "mape": {r.modelo: None if r.mape is None or pd.isna(r.mape)
                             else round(r.mape, 4) for r in g.itertuples()},
                }
                for h, g in group.groupby("horizonte")
            }
            for serie, group in results_df.groupby("serie", sort=False)
        },
    }
    if write:
        storage.write_bytes(RESULTS_PATH, _parquet(results_df, RESULTS_SCHEMA))
        storage.write_bytes(PREDICTIONS_PATH, _parquet(predictions_df, PREDICTIONS_SCHEMA))
        storage.write_bytes(BACKTEST_PATH, _parquet(detail_df, BACKTEST_SCHEMA))
        storage.write_bytes(f"{REPORT_PREFIX}/datos_hasta={report['datos_hasta']}.json",
                            (json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode())
    if not_converged:
        log.warning("%d ajustes del backtest no convergieron (se usaron igual)", not_converged)
    return report
