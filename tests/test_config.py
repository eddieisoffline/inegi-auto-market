import dataclasses

import pytest

from inegi_market.config import Settings


def test_defaults_use_local_lake():
    s = Settings()
    assert (s.backend, s.data_dir, s.bucket, s.project) == ("local", "data/lake", "", "")


def test_from_env_reads_prefixed_variables(monkeypatch):
    monkeypatch.setenv("INEGI_MARKET_BACKEND", "gcs")
    monkeypatch.setenv("INEGI_MARKET_DATA_DIR", "/tmp/lake")
    monkeypatch.setenv("INEGI_MARKET_BUCKET", "mi-bucket")
    monkeypatch.setenv("INEGI_MARKET_PROJECT", "mi-proyecto")
    assert Settings.from_env() == Settings("gcs", "/tmp/lake", "mi-bucket", "mi-proyecto")


def test_settings_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        Settings().backend = "gcs"


def test_tokens_never_appear_in_printed_config(monkeypatch):
    monkeypatch.setenv("INEGI_TOKEN", "token-inegi-de-prueba")
    monkeypatch.setenv("BANXICO_TOKEN", "token-banxico-de-prueba")
    printed = repr(Settings.from_env())
    assert "token-inegi-de-prueba" not in printed
    assert "token-banxico-de-prueba" not in printed
