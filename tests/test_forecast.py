import io
import json
from datetime import date

import numpy as np
import pandas as pd
import pyarrow.parquet as pq
import pytest
from conftest import MemoryStorage

from inegi_market import forecast as fc
from inegi_market.cli import main
from inegi_market.forecast import (
    BACKTEST_PATH,
    BASELINE,
    PREDICTIONS_PATH,
    RESULTS_PATH,
    Forecast,
    ForecastConfig,
    ForecastError,
    adjust_2020,
    backtest,
    mase_scale,
    metrics,
    origins,
    run_forecast,
    sarimax_fx,
    seasonal_naive,
    summarize,
)
from inegi_market.storage import LocalStorage


def monthly(values, start="2018-01-01"):
    index = pd.date_range(start, periods=len(values), freq="MS")
    return pd.Series(np.asarray(values, dtype="float64"), index=index)


def synthetic(start="2015-01-01", end="2023-12-01", level=1000.0, seed=1):
    """Serie con tendencia, estacionalidad y ruido; 2020-04 con una caída como la real."""
    rng = np.random.default_rng(seed)
    index = pd.date_range(start, end, freq="MS")
    t = np.arange(len(index))
    month = index.month.to_numpy()
    season = 1 + 0.2 * np.sin(2 * np.pi * (month - 1) / 12) + 0.3 * (month == 12)
    values = level * (1 + 0.004 * t) * season * rng.normal(1, 0.02, len(index))
    values[np.asarray(index == pd.Timestamp("2020-04-01"))] *= 0.4
    return pd.Series(values.round(), index=index)


# --- métricas y línea base --------------------------------------------------------------------


def test_metrics_by_hand():
    errors = pd.DataFrame({"real": [100.0, 200.0], "pronostico": [110.0, 190.0],
                           "escala_mase": [10.0, 20.0]})
    result = metrics(errors)
    assert result["mae"] == 10 and result["rmse"] == 10
    assert result["mase"] == pytest.approx((10 / 10 + 10 / 20) / 2)  # 0.75
    assert result["mape"] == pytest.approx((10 / 100 + 10 / 200) / 2)  # 0.075


def test_mape_is_omitted_when_there_are_zeros():
    errors = pd.DataFrame({"real": [0.0, 10.0], "pronostico": [1.0, 9.0],
                           "escala_mase": [1, 1]})
    assert metrics(errors)["mape"] is None
    assert metrics(errors)["rmse"] == 1


def test_seasonal_naive_repeats_the_same_month_of_last_year():
    y = monthly(range(1, 25))  # 2018-01 = 1 ... 2019-12 = 24
    assert seasonal_naive(y, 6).mean.tolist() == [13, 14, 15, 16, 17, 18]
    with pytest.raises(ForecastError):
        seasonal_naive(y, 13)


# --- origen móvil sin fuga --------------------------------------------------------------------


def test_origins_cover_targets_from_eval_start():
    y = monthly(range(1, 67))  # 2018-01 a 2023-06
    found = origins(y, date(2023, 1, 1), max_h=6)
    assert found[0] == pd.Timestamp("2022-07-01") and found[-1] == pd.Timestamp("2023-05-01")


def test_backtest_never_shows_a_model_data_after_the_origin():
    y, fx = synthetic(), synthetic(level=17, seed=2)
    seen = []

    def spy(train, steps, known_fx):
        seen.append((train.index[-1], known_fx.index[-1], len(train)))
        return Forecast(np.full(steps, train.iloc[-1]))

    config = ForecastConfig(eval_start=date(2023, 7, 1))
    detail, _ = backtest("x", y, fx, config, {BASELINE: seasonal_naive, "espia": spy})
    assert all(last_y == last_fx for last_y, last_fx, _ in seen)
    by_origin = detail[detail["modelo"] == "espia"].groupby("origen")["objetivo"].min()
    assert all(pd.Timestamp(t) > pd.Timestamp(o) for o, t in by_origin.items())
    assert detail["objetivo"].min() == date(2023, 7, 1)
    assert detail["objetivo"].max() == date(2023, 12, 1)
    per_h = detail[detail["modelo"] == "espia"].groupby("h")["objetivo"].apply(set)
    assert all(targets == per_h[1] for targets in per_h)  # mismos meses en cada horizonte


def test_baseline_trains_on_raw_data_and_the_models_on_the_adjusted_series():
    y = synthetic()
    received = {}

    def recorder(name):
        def model(train, steps, known_fx):
            received[name] = train
            return Forecast(np.ones(steps))
        return model

    config = ForecastConfig(eval_start=date(2023, 12, 1))
    backtest("x", y, None, config, {BASELINE: recorder(BASELINE), "m": recorder("m")})
    april = pd.Timestamp("2020-04-01")
    assert received[BASELINE][april] == y[april]
    assert received["m"][april] != y[april]


# --- tratamiento de 2020 ----------------------------------------------------------------------


def test_adjust_2020_interpolates_the_ratio_to_2019():
    y = synthetic()
    adjusted = adjust_2020(y)
    feb = y["2020-02-01"] / y["2019-02-01"]
    jul = y["2020-07-01"] / y["2019-07-01"]
    for step, month in enumerate([3, 4, 5, 6], start=1):
        ratio = feb + (jul - feb) * step / 5
        expected = y[f"2019-{month:02d}-01"] * ratio
        assert adjusted[f"2020-{month:02d}-01"] == pytest.approx(expected)
    untouched = adjusted.index.difference(fc.SHOCK_2020)
    assert (adjusted[untouched] == y[untouched]).all()
    assert adjust_2020(y[:"2019-12-01"]).equals(y[:"2019-12-01"])  # sin 2020: no cambia


def test_mase_scale_ignores_differences_that_touch_2020():
    y = monthly([100.0] * 12 + [110.0] * 12 + [10.0] * 12 + [120.0] * 12, start="2018-01-01")
    # 2019-2018: 10 cada mes. 2020 y 2021 tocan 2020: fuera.
    assert mase_scale(y) == 10


# --- tipo de cambio rezagado ------------------------------------------------------------------


def test_sarimax_fx_uses_the_exchange_rate_from_six_months_before(monkeypatch):
    captured = {}

    def fake(y, steps, exog=None, exog_future=None):
        captured.update(y=y, exog=exog, future=exog_future)
        return Forecast(np.ones(steps))

    monkeypatch.setattr(fc, "_sarimax", fake)
    y = synthetic(end="2023-06-01")
    fx = synthetic(level=17, seed=3)[:"2023-06-01"]
    sarimax_fx(y, 6, fx)
    # meses 2023-07 a 2023-12 -> tipo de cambio de 2023-01 a 2023-06
    expected_future = np.log(fx["2023-01-01":"2023-06-01"]).to_numpy()
    assert np.allclose(captured["future"].ravel(), expected_future)
    first = captured["y"].index[0]
    assert first == y.index[0] + pd.DateOffset(months=6)  # los primeros 6 meses no tienen rezago
    assert captured["exog"][0, 0] == pytest.approx(np.log(fx[y.index[0]]))


def test_sarimax_fx_needs_the_exchange_rate_up_to_the_origin():
    y = synthetic(end="2023-06-01")
    with pytest.raises(ForecastError, match="no llega al origen"):
        sarimax_fx(y, 6, synthetic(level=17)[:"2023-05-01"])


# --- resumen ----------------------------------------------------------------------------------


def test_summary_marks_the_winner_and_the_improvement_over_the_baseline():
    rows = []
    for model, error in ((BASELINE, 20.0), ("bueno", 10.0)):
        for h in range(1, 7):
            rows.append({"serie": "x", "modelo": model, "h": h,
                         "objetivo": date(2023, h, 1), "real": 100.0,
                         "pronostico": 100.0 + error, "escala_mase": 10.0})
    summary = summarize(pd.DataFrame(rows), ForecastConfig())
    good = summary[(summary["modelo"] == "bueno") & (summary["horizonte"] == 6)].iloc[0]
    assert (good["mase"], good["mejora_mase_vs_base"], good["es_ganador"]) == (1.0, 0.5, True)
    base = summary[summary["modelo"] == BASELINE]
    assert (base["mejora_mase_vs_base"] == 0).all() and not base["es_ganador"].any()


# --- ejecución completa con modelos reales y datos sintéticos ---------------------------------


def lake_with_synthetic_sales(storage):
    index = pd.date_range("2015-01-01", "2023-12-01", freq="MS")
    brands = {"Grande": synthetic(level=900), "Chica": synthetic(level=100, seed=5),
              "Nueva": synthetic(level=50, seed=6)["2019-01-01":]}
    rows = [{"periodo": d.date(), "marca": b, "unidades": int(v)}
            for b, s in brands.items() for d, v in s.items()]
    df = pd.DataFrame(rows)
    for year, part in df.groupby(pd.to_datetime(df["periodo"]).dt.year):
        buffer = io.BytesIO()
        part.to_parquet(buffer, index=False)
        path = f"curated/venta/publication_date=2024-01-10/anio={year}/part-0.parquet"
        storage.write_bytes(path, buffer.getvalue())
    fx = pd.DataFrame({"periodo": [d.date() for d in index],
                       "tipo_cambio_promedio": synthetic(level=17, seed=7).to_numpy()})
    buffer = io.BytesIO()
    fx.to_parquet(buffer, index=False)
    storage.write_bytes("curated/tipo_cambio_mensual/part-0.parquet", buffer.getvalue())
    return storage


def test_brand_series_start_at_their_first_sale():
    series = fc.load_sales(lake_with_synthetic_sales(MemoryStorage()), top_brands=3)
    assert list(series) == ["nacional", "Grande", "Chica", "Nueva"]
    assert series["Nueva"].index[0] == pd.Timestamp("2019-01-01")
    assert series["Grande"].index[0] == pd.Timestamp("2015-01-01")


def test_run_forecast_end_to_end():
    storage = lake_with_synthetic_sales(MemoryStorage())
    config = ForecastConfig(eval_start=date(2023, 10, 1), top_brands=1, workers=1)
    report = run_forecast(storage, config, today=lambda: date(2024, 1, 15))

    results = pq.read_table(io.BytesIO(storage.read_bytes(RESULTS_PATH))).to_pandas()
    assert set(results["serie"]) == {"nacional", "Grande"}
    assert set(results["modelo"]) == {BASELINE, "ets", "sarima", "sarimax_fx"}
    assert set(results["horizonte"]) == {3, 6}
    assert (results.groupby(["serie", "horizonte"])["es_ganador"].sum() >= 1).all()
    assert (results["datos_hasta"] == date(2023, 12, 1)).all() and results["ajuste_2020"].all()

    predictions = pq.read_table(io.BytesIO(storage.read_bytes(PREDICTIONS_PATH))).to_pandas()
    is_national = predictions["serie"] == "nacional"
    national = predictions[is_national & (predictions["modelo"] == "sarima")]
    assert national["periodo"].tolist() == [date(2024, m, 1) for m in range(1, 7)]
    assert (national["limite_inferior_80"] < national["pronostico"]).all()
    assert (national["pronostico"] < national["limite_superior_80"]).all()
    naive = predictions[predictions["modelo"] == BASELINE]
    assert naive["limite_inferior_80"].isna().all()

    detail = pq.read_table(io.BytesIO(storage.read_bytes(BACKTEST_PATH))).to_pandas()
    assert detail["objetivo"].min() == date(2023, 10, 1)
    saved = json.loads(storage.read_bytes("reports/forecast/datos_hasta=2023-12.json"))
    assert saved == report and set(report["series"]) == {"nacional", "Grande"}


def test_cli_forecast_without_exchange_rate_exits_1(tmp_path, monkeypatch):
    monkeypatch.setenv("INEGI_MARKET_BACKEND", "local")
    monkeypatch.setenv("INEGI_MARKET_DATA_DIR", str(tmp_path / "lake"))
    storage = lake_with_synthetic_sales(LocalStorage(str(tmp_path / "lake")))
    (tmp_path / "lake" / "curated" / "tipo_cambio_mensual" / "part-0.parquet").unlink()
    assert not storage.exists("curated/tipo_cambio_mensual/part-0.parquet")
    with pytest.raises(SystemExit) as exc:
        main(["forecast", "--workers", "1"])
    assert exc.value.code == 1
