import logging
from decimal import Decimal

import pytest
from api_fakes import INEGI_TOKEN, FakeApi, inegi_body

from inegi_market.sources.inegi_api import (
    INDICATORS,
    ApiError,
    MissingTokenError,
    Observation,
    fetch_indicator,
    indicator_url,
    parse_observations,
    read_token,
)


def no_sleep(seconds):
    pass


def test_indicators_are_the_confirmed_national_totals():
    assert INDICATORS == {"venta": "6207131346", "produccion": "6207131345",
                          "exportacion": "6207131349"}


def test_url_follows_the_bise_endpoint():
    assert indicator_url("6207131346", "TOKEN") == (
        "https://www.inegi.org.mx/app/api/indicadores/desarrolladores/jsonxml/INDICATOR/"
        "6207131346/es/00/false/BISE/2.0/TOKEN?type=json"
    )


def test_values_with_twenty_decimals_are_read_as_exact_decimals():
    body = inegi_body({(2026, 9): "129274.00000000000000000000",
                       (2026, 8): "0.30000000000000000001"})
    assert parse_observations(body, "6207131346") == [
        Observation(2026, 8, Decimal("0.30000000000000000001"), "2"),
        Observation(2026, 9, Decimal("129274"), "2"),
    ]


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b'{"Series": []}', "Series"),
        (b"<html>error</html>", "Series"),
        (inegi_body({(2026, 9): "N/D"}), "no numérico"),
        (inegi_body({(2026, 9): "1"}).replace(b"2026/09", b"2026-09"), "TIME_PERIOD"),
        (inegi_body({(2026, 9): "1"}).replace(b"2026/09", b"2026/13"), "inválido"),
    ],
    ids=["sin-series", "html", "valor-no-numerico", "periodo-con-guion", "mes-13"],
)
def test_unexpected_responses_are_api_errors(body, message):
    with pytest.raises(ApiError, match=message):
        parse_observations(body, "6207131346")


def test_missing_token_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("INEGI_TOKEN", raising=False)
    with pytest.raises(MissingTokenError, match="INEGI_TOKEN"):
        read_token("INEGI_TOKEN")
    monkeypatch.setenv("INEGI_TOKEN", "   ")
    with pytest.raises(MissingTokenError):
        read_token("INEGI_TOKEN")


def test_fetch_indicator_returns_body_and_observations():
    body = inegi_body({(2026, 9): "129274.00000000000000000000"})
    api = FakeApi({"6207131346": [(200, body)]})
    raw, observations = fetch_indicator(api, "6207131346", INEGI_TOKEN, no_sleep)
    assert raw == body and observations[0].valor == 129274
    ((url, headers),) = api.calls
    assert url == indicator_url("6207131346", INEGI_TOKEN)
    assert headers == {"Accept": "application/json"}


def test_http_errors_never_show_the_token(caplog):
    api = FakeApi({"6207131346": [(401, b"token invalido")]})
    with pytest.raises(ApiError) as exc:
        fetch_indicator(api, "6207131346", INEGI_TOKEN, no_sleep)
    assert "HTTP 401" in str(exc.value) and INEGI_TOKEN not in str(exc.value)


def test_network_errors_are_retried_and_scrubbed(caplog):
    caplog.set_level(logging.WARNING)
    leaked = OSError(f"no se pudo abrir {indicator_url('6207131346', INEGI_TOKEN)}")
    api = FakeApi({"6207131346": [leaked]})
    with pytest.raises(ApiError) as exc:
        fetch_indicator(api, "6207131346", INEGI_TOKEN, no_sleep)
    assert len(api.calls) == 3
    assert INEGI_TOKEN not in str(exc.value) and "***" in str(exc.value)
    assert all(INEGI_TOKEN not in m for m in caplog.messages)
