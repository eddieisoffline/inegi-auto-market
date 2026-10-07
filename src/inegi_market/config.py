"""Configuración del pipeline, leída de variables de entorno INEGI_MARKET_*.

Los tokens de las APIs (INEGI_TOKEN, BANXICO_TOKEN) no forman parte de esta
configuración: se leen del entorno solo donde se usan, así nunca aparecen al
imprimir o registrar la configuración.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    backend: str = "local"      # almacenamiento: "local" o "gcs"
    data_dir: str = "data/lake"  # raíz del lake con backend local
    bucket: str = ""            # bucket con backend gcs
    project: str = ""           # proyecto de GCP (para el warehouse)

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            backend=os.getenv("INEGI_MARKET_BACKEND", "local"),
            data_dir=os.getenv("INEGI_MARKET_DATA_DIR", "data/lake"),
            bucket=os.getenv("INEGI_MARKET_BUCKET", ""),
            project=os.getenv("INEGI_MARKET_PROJECT", ""),
        )
