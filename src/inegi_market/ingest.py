"""Ingesta de una foto del RAIAVL (zip del INEGI) a la capa raw.

Cada publicación se guarda tal como llegó, en una partición por producto y por
fecha de publicación (`modified` de los metadatos que trae el propio zip):

    raw/raiavl/<producto>/publication_date=YYYY-MM-DD/
        conjunto_de_datos_raiavl_mensual_<producto>_csv.zip   bytes originales
        metadatos_raiavl_<producto>_mensual_<años>.txt        copia del metadato
        manifest.json                                          se escribe al final

El manifiesto marca que la foto quedó completa y guarda, además del sha256 del
zip, el sha256 de cada archivo ya descomprimido. Una foto guardada nunca se
sobrescribe. Si llega otra con la misma fecha:
- mismo zip: no se hace nada;
- otro zip con los mismos archivos por dentro (el INEGI lo regeneró con otra
  compresión, otras fechas internas u otro orden): no se escribe nada y se avisa;
- contenido distinto (revisión silenciosa): la ingesta se detiene con error y dice
  qué archivos cambiaron.
El producto y la fecha salen del contenido del zip, no de su nombre (un zip
subido a mano puede llamarse como sea).
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath

from .storage import Storage

log = logging.getLogger(__name__)

PRODUCTS = ("venta", "produccion", "exportacion", "hibrido")
RAW_PREFIX = "raw/raiavl"
ZIP_NAME = "conjunto_de_datos_raiavl_mensual_{product}_csv.zip"

_METADATA_NAME = re.compile(r"metadatos/metadatos_raiavl_([a-z]+)_mensual_[^/]*\.txt")
_DATA_NAME = re.compile(r"conjunto_de_datos/raiavl_([a-z]+)_mensual_tr_cifra_\d{4}\.csv")
_ISO_DATE = r"\d{4}-\d{2}-\d{2}"
_COMPRESSION = {zipfile.ZIP_STORED: "stored", zipfile.ZIP_DEFLATED: "deflated"}


class IngestError(ValueError):
    """La foto no se puede ingerir; el mensaje dice por qué."""


class SnapshotConflictError(IngestError):
    """Ya hay una foto guardada con la misma fecha y otro contenido."""


@dataclass(frozen=True)
class SnapshotInfo:
    product: str
    modified: date
    temporal: str
    temporal_start: date
    temporal_end: date
    metadata_name: str
    metadata_bytes: bytes
    compression: str
    files_sha256: dict[str, str]  # sha256 de cada archivo descomprimido, por ruta


@dataclass(frozen=True)
class IngestResult:
    status: str  # "ingested", "already_present" o "same_content"
    product: str
    publication_date: date
    partition: str
    sha256: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def partition_path(product: str, publication_date: date) -> str:
    return f"{RAW_PREFIX}/{product}/publication_date={publication_date.isoformat()}/"


def _metadata_field(text: str, key: str, name: str) -> str:
    values = re.findall(rf"^{key}:[ \t]*(.*?)[ \t]*\r?$", text, re.MULTILINE)
    if len(values) != 1:
        raise IngestError(f"{name}: se esperaba una línea '{key}:' y hay {len(values)}")
    return values[0]


def _parse_date(value: str, what: str) -> date:
    try:
        if not re.fullmatch(_ISO_DATE, value):
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise IngestError(f"{what} no es una fecha AAAA-MM-DD válida: {value!r}") from None


def _compression(infos: list[zipfile.ZipInfo]) -> str:
    kinds = {info.compress_type for info in infos}
    if len(kinds) > 1:
        return "mixed"
    (kind,) = kinds
    return _COMPRESSION.get(kind, f"type_{kind}")


def _hash_members(zf: zipfile.ZipFile, files: list[zipfile.ZipInfo]) -> dict[str, str]:
    """sha256 de cada archivo descomprimido. Al leerlo completo, zipfile verifica el CRC."""
    hashes = {}
    for info in files:
        digest = hashlib.sha256()
        with zf.open(info) as member:
            while chunk := member.read(1 << 20):
                digest.update(chunk)
        hashes[info.filename] = digest.hexdigest()
    return dict(sorted(hashes.items()))


def read_snapshot_info(data: bytes) -> SnapshotInfo:
    """Valida el empaquetado del zip y lee producto y fechas de su metadato."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise IngestError(f"no es un zip válido ({exc})") from None
    with zf:
        files = [info for info in zf.infolist() if not info.is_dir()]
        names = [info.filename for info in files]
        try:
            files_sha256 = _hash_members(zf, files)
        except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
            raise IngestError(
                f"el zip está corrupto o usa una compresión no soportada ({exc})"
            ) from None

        metadata = [n for n in names if n.startswith("metadatos/") and n.endswith(".txt")]
        if len(metadata) != 1:
            raise IngestError(
                f"el zip debe traer exactamente un metadatos/*.txt y trae {len(metadata)}"
            )
        metadata_name = metadata[0]
        match = _METADATA_NAME.fullmatch(metadata_name)
        if not match or match.group(1) not in PRODUCTS:
            raise IngestError(f"no se reconoce el producto en el metadato {metadata_name!r}")
        product = match.group(1)

        data_products = {m.group(1) for n in names if (m := _DATA_NAME.fullmatch(n))}
        if not data_products:
            raise IngestError(
                "el zip no trae CSV con el nombre esperado en conjunto_de_datos/ "
                f"(raiavl_{product}_mensual_tr_cifra_AAAA.csv)"
            )
        if data_products != {product}:
            raise IngestError(
                f"el metadato dice {product!r} pero los CSV de conjunto_de_datos/ son de "
                f"{sorted(data_products)}"
            )

        metadata_bytes = zf.read(metadata_name)

    try:
        text = metadata_bytes.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise IngestError(f"{metadata_name} no está en UTF-8 ({exc})") from None
    modified = _parse_date(_metadata_field(text, "modified", metadata_name), "modified")
    temporal = _metadata_field(text, "temporal", metadata_name)
    period = re.fullmatch(rf"({_ISO_DATE})-({_ISO_DATE})", temporal)
    if not period:
        raise IngestError(f"temporal no tiene la forma AAAA-MM-DD-AAAA-MM-DD: {temporal!r}")
    return SnapshotInfo(
        product=product,
        modified=modified,
        temporal=temporal,
        temporal_start=_parse_date(period.group(1), "inicio de temporal"),
        temporal_end=_parse_date(period.group(2), "fin de temporal"),
        metadata_name=metadata_name,
        metadata_bytes=metadata_bytes,
        compression=_compression(files),
        files_sha256=files_sha256,
    )


def _describe_changes(stored: dict[str, str] | None, new: dict[str, str]) -> str:
    if stored is None:
        return "la foto guardada no registra el sha256 de cada archivo"
    groups = {
        "cambiaron": sorted(n for n in stored.keys() & new.keys() if stored[n] != new[n]),
        "nuevos": sorted(new.keys() - stored.keys()),
        "faltan": sorted(stored.keys() - new.keys()),
    }
    return "; ".join(f"{label}: {', '.join(names)}" for label, names in groups.items() if names)


def ingest_zip(
    data: bytes,
    original_name: str,
    storage: Storage,
    now: Callable[[], datetime] = _utcnow,
    expected_product: str | None = None,
    origin: dict[str, str] | None = None,
) -> IngestResult:
    """Guarda la foto en raw/ si no existe; nunca sobrescribe una foto completa.

    `expected_product` lo usa la descarga: el zip de una URL debe ser de su producto.
    `origin` queda en el manifiesto (de dónde vino el zip); por defecto, un archivo local.
    """
    info = read_snapshot_info(data)
    if expected_product is not None and info.product != expected_product:
        raise IngestError(
            f"se esperaba un zip de {expected_product!r} y el contenido es de {info.product!r}"
        )
    sha256 = hashlib.sha256(data).hexdigest()
    partition = partition_path(info.product, info.modified)
    manifest_path = partition + "manifest.json"
    label = f"{info.product} {info.modified.isoformat()}"

    if storage.exists(manifest_path):
        stored = json.loads(storage.read_bytes(manifest_path))
        if stored["sha256"] == sha256:
            log.info("%s: ya existe con el mismo sha256; no se hace nada", label)
            return IngestResult("already_present", info.product, info.modified, partition, sha256)
        stored_files = stored.get("files_sha256")
        if stored_files == info.files_sha256:
            log.warning(
                "%s: mismo contenido con otro empaquetado (zip guardado sha256 %s, nuevo %s); "
                "se conserva la foto guardada y no se escribe nada",
                label, stored["sha256"][:12], sha256[:12],
            )
            return IngestResult("same_content", info.product, info.modified, partition, sha256)
        raise SnapshotConflictError(
            f"{label}: ya hay una foto guardada con otro contenido y la misma fecha 'modified' "
            f"({_describe_changes(stored_files, info.files_sha256)}); no se sobrescribe. "
            f"sha256 del zip guardado {stored['sha256']}, del nuevo {sha256}."
        )

    # Sin manifiesto la partición no está completa (p. ej., un intento previo que se
    # cortó): se escribe todo de nuevo y el manifiesto al final.
    zip_name = ZIP_NAME.format(product=info.product)
    metadata_file = PurePosixPath(info.metadata_name).name
    storage.write_bytes(partition + zip_name, data)
    storage.write_bytes(partition + metadata_file, info.metadata_bytes)
    manifest = {
        "product": info.product,
        "modified": info.modified.isoformat(),
        "temporal": info.temporal,
        "temporal_start": info.temporal_start.isoformat(),
        "temporal_end": info.temporal_end.isoformat(),
        "sha256": sha256,
        "size_bytes": len(data),
        "compression": info.compression,
        "ingested_at": now().isoformat(timespec="seconds"),
        "original_name": original_name,
        "zip_name": zip_name,
        "metadata_name": metadata_file,
        "files_sha256": info.files_sha256,
        "origin": origin or {"type": "file"},
    }
    body = json.dumps(manifest, ensure_ascii=False, indent=2) + "\n"
    storage.write_bytes(manifest_path, body.encode("utf-8"))
    log.info(
        "%s: foto guardada en %s (%s, %d bytes, sha256 %s)",
        label, partition, info.compression, len(data), sha256[:12],
    )
    return IngestResult("ingested", info.product, info.modified, partition, sha256)


def find_zips(path: str | Path) -> list[Path]:
    """Un zip, o todos los *.zip de una carpeta en orden alfabético."""
    p = Path(path)
    if p.is_dir():
        zips = sorted(p.glob("*.zip"))
        if not zips:
            raise IngestError(f"no hay archivos .zip en {p}")
        return zips
    if p.is_file():
        return [p]
    raise IngestError(f"no existe: {p}")


def ingest_file(
    path: str | Path, storage: Storage, now: Callable[[], datetime] = _utcnow
) -> IngestResult:
    """Ingiere un zip local. Del nombre original solo se guarda el archivo, sin carpetas."""
    p = Path(path)
    return ingest_zip(p.read_bytes(), p.name, storage, now)
