import io
from datetime import date
from decimal import Decimal

import pandas as pd
import pyarrow.parquet as pq
import pytest
from api_fakes import BANXICO_TOKEN, FakeApi, banxico_body
from conftest import MemoryStorage

from inegi_market.cli import main
from inegi_market.fx import DAILY_PATH, MONTHLY_PATH, update_fx
from inegi_market.sources.banxico_api import (
    DailyRate,
    fetch_rates,
    monthly_rates,
    parse_rates,
    series_url,
)
from inegi_market.sources.inegi_api import ApiError, MissingTokenError
from inegi_market.storage import LocalStorage

SEPT_OCT = [("29/09/2026", "17.9000"), ("30/09/2026", "18.0010"), ("01/10/2026", "N/E"),
            ("02/10/2026", "17.9780"), ("05/10/2026", "17.9500")]


def no_sleep(seconds):
    pass


def test_dates_and_text_values_are_parsed_and_missing_values_counted():
    rates, skipped = parse_rates(banxico_body(SEPT_OCT))
    assert rates[0] == DailyRate(date(2026, 9, 29), Decimal("17.9000"))
    assert [r.fecha for r in rates][-1] == date(2026, 10, 5)
    assert skipped == 1  # "N/E": sin dato


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b'{"bmx": {}}', "bmx.series"),
        (banxico_body([("2026-10-02", "17.9")]), "fecha inesperada"),
        (banxico_body([("02/10/2026", "17.9"), ("02/10/2026", "18.0")]), "repetida"),
    ],
)
def test_unexpected_responses_are_api_errors(body, message):
    with pytest.raises(ApiError, match=message):
        parse_rates(body)


def test_monthly_average_and_last_business_day():
    rates, _ = parse_rates(banxico_body(SEPT_OCT))
    september, october = monthly_rates(rates, retrieved=date(2026, 10, 8))
    assert september.promedio == Decimal("17.9505")  # (17.9000 + 18.0010) / 2
    assert (september.ultimo_dia, september.fecha_ultimo_dia) == (Decimal("18.0010"),
                                                                  date(2026, 9, 30))
    assert (september.dias_con_dato, september.mes_completo) == (2, True)
    assert october.promedio == Decimal("17.9640")  # (17.9780 + 17.9500) / 2
    assert (october.dias_con_dato, october.mes_completo) == (2, False)  # octubre sigue abierto


def test_fetch_rates_sends_the_token_in_a_header():
    api = FakeApi({"SF43718": [(200, banxico_body(SEPT_OCT))]})
    _, rates, skipped = fetch_rates(api, BANXICO_TOKEN, end=date(2026, 10, 8), sleep=no_sleep)
    ((url, headers),) = api.calls
    assert url == series_url(date(2005, 1, 1), date(2026, 10, 8))
    assert url.endswith("/series/SF43718/datos/2005-01-01/2026-10-08")
    assert headers == {"Bmx-Token": BANXICO_TOKEN, "Accept": "application/json"}
    assert BANXICO_TOKEN not in url and len(rates) == 4 and skipped == 1


def test_http_errors_never_show_the_token():
    api = FakeApi({"SF43718": [(401, b"")]})
    with pytest.raises(ApiError) as exc:
        fetch_rates(api, BANXICO_TOKEN, end=date(2026, 10, 8), sleep=no_sleep)
    assert "HTTP 401" in str(exc.value) and BANXICO_TOKEN not in str(exc.value)


def read_parquet(storage, path):
    return pq.read_table(io.BytesIO(storage.read_bytes(path)))


def test_update_fx_writes_raw_and_curated(monkeypatch):
    monkeypatch.setenv("BANXICO_TOKEN", BANXICO_TOKEN)
    storage = MemoryStorage()
    api = FakeApi({"SF43718": [(200, banxico_body(SEPT_OCT))]})
    result = update_fx(api, storage, today=lambda: date(2026, 10, 8), sleep=no_sleep)
    assert (result.days, result.months, result.skipped) == (4, 2, 1)
    assert storage.exists("raw/banxico/sf43718/retrieved_at=2026-10-08/response.json")

    daily = read_parquet(storage, DAILY_PATH)
    assert str(daily.schema.field("tipo_cambio").type) == "decimal128(10, 4)"
    assert daily.column("tipo_cambio").to_pylist()[0] == Decimal("17.9000")
    monthly = read_parquet(storage, MONTHLY_PATH).to_pandas()
    assert list(monthly.columns) == [
        "anio", "mes", "periodo", "tipo_cambio_promedio", "tipo_cambio_ultimo_dia",
        "fecha_ultimo_dia", "dias_con_dato", "mes_completo", "consultado_el"]
    assert monthly.loc[0, "tipo_cambio_promedio"] == Decimal("17.9505")
    assert monthly["mes_completo"].tolist() == [True, False]


def test_update_fx_without_token_makes_no_calls(monkeypatch):
    monkeypatch.delenv("BANXICO_TOKEN", raising=False)
    api = FakeApi({"SF43718": [(200, banxico_body(SEPT_OCT))]})
    with pytest.raises(MissingTokenError, match="BANXICO_TOKEN"):
        update_fx(api, MemoryStorage())
    assert api.calls == []


def test_update_fx_rejects_an_empty_series(monkeypatch):
    monkeypatch.setenv("BANXICO_TOKEN", BANXICO_TOKEN)
    api = FakeApi({"SF43718": [(200, banxico_body([("01/10/2026", "N/E")]))]})
    with pytest.raises(ApiError, match="ningún dato"):
        update_fx(api, MemoryStorage(), sleep=no_sleep)


def test_cli_fx(tmp_path, monkeypatch):
    monkeypatch.setenv("INEGI_MARKET_BACKEND", "local")
    monkeypatch.setenv("INEGI_MARKET_DATA_DIR", str(tmp_path / "lake"))
    monkeypatch.setenv("BANXICO_TOKEN", BANXICO_TOKEN)
    main(["fx"], client=FakeApi({"SF43718": [(200, banxico_body(SEPT_OCT))]}))
    lake = LocalStorage(str(tmp_path / "lake"))
    assert len(pd.read_parquet(tmp_path / "lake" / MONTHLY_PATH)) == 2
    assert lake.exists(DAILY_PATH)

    monkeypatch.delenv("BANXICO_TOKEN")
    with pytest.raises(SystemExit) as exc:
        main(["fx"], client=FakeApi({"SF43718": [(200, b"")]}))
    assert exc.value.code == 1
