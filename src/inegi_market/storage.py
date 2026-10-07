"""Almacenamiento del data lake: carpeta local o bucket de Cloud Storage.

Ambas implementaciones exponen la misma interfaz, así el resto del pipeline
no sabe dónde corre y las pruebas locales equivalen a las de la nube.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from .config import Settings


class Storage(Protocol):
    def write_bytes(self, path: str, data: bytes) -> None: ...
    def read_bytes(self, path: str) -> bytes: ...
    def exists(self, path: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...


class LocalStorage:
    def __init__(self, root: str):
        self.root = Path(root)

    def write_bytes(self, path: str, data: bytes) -> None:
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)  # escritura atómica: nunca queda un archivo a medias

    def read_bytes(self, path: str) -> bytes:
        return (self.root / path).read_bytes()

    def exists(self, path: str) -> bool:
        return (self.root / path).exists()

    def list(self, prefix: str) -> list[str]:
        """Rutas que empiezan con `prefix` (misma semántica que GCS)."""
        if prefix == "" or prefix.endswith("/"):
            start = self.root / prefix
        else:
            start = (self.root / prefix).parent
        if not start.exists():
            return []
        found = []
        for p in start.rglob("*"):
            if p.is_file() and not p.name.endswith(".tmp"):
                rel = p.relative_to(self.root).as_posix()
                if rel.startswith(prefix):
                    found.append(rel)
        return sorted(found)


class GCSStorage:
    def __init__(self, bucket: str, client: Any = None):
        if client is None:
            from google.cloud import storage  # import tardío: es dependencia opcional

            client = storage.Client()
        self.bucket = client.bucket(bucket)

    def write_bytes(self, path: str, data: bytes) -> None:
        self.bucket.blob(path).upload_from_string(data)

    def read_bytes(self, path: str) -> bytes:
        return self.bucket.blob(path).download_as_bytes()

    def exists(self, path: str) -> bool:
        return self.bucket.blob(path).exists()

    def list(self, prefix: str) -> list[str]:
        return sorted(b.name for b in self.bucket.list_blobs(prefix=prefix))


def get_storage(settings: Settings) -> Storage:
    if settings.backend == "gcs":
        if not settings.bucket:
            raise ValueError("INEGI_MARKET_BUCKET es obligatorio con backend gcs")
        return GCSStorage(settings.bucket)
    if settings.backend != "local":
        raise ValueError(
            f"INEGI_MARKET_BACKEND desconocido: {settings.backend!r} (usa 'local' o 'gcs')"
        )
    return LocalStorage(settings.data_dir)
