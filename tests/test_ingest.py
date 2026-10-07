import hashlib
import json
import logging
import re
import zipfile
from datetime import date, datetime, timezone

import pytest
from conftest import PRODUCTS, SNAPSHOTS, build_zip, fixture_dir, fixture_files, make_zip

from inegi_market.cli import main
from inegi_market.ingest import (
    IngestError,
    SnapshotConflictError,
    find_zips,
    ingest_file,
    ingest_zip,
    partition_path,
    read_snapshot_info,
)
from inegi_market.storage import LocalStorage

NOW = datetime(2026, 10, 7, 18, 30, tzinfo=timezone.utc)
VENTA_META = "metadatos/metadatos_raiavl_venta_mensual_2005_2026.txt"
VENTA_PARTITION = "raw/raiavl/venta/publication_date=2026-10-07/"
VENTA_ZIP = VENTA_PARTITION + "conjunto_de_datos_raiavl_mensual_venta_csv.zip"
VENTA_2026 = "conjunto_de_datos/raiavl_venta_mensual_tr_cifra_2026.csv"


def fixed_now():
    return NOW


class MemoryStorage:
    """Storage en memoria que registra cada escritura."""

    def __init__(self):
        self.files, self.writes = {}, []

    def write_bytes(self, path, data):
        self.files[path] = data
        self.writes.append(path)

    def read_bytes(self, path):
        return self.files[path]

    def exists(self, path):
        return path in self.files

    def list(self, prefix):
        return sorted(p for p in self.files if p.startswith(prefix))


def snapshot_zip(snapshot="2026-10-07", product="venta", compression=zipfile.ZIP_STORED):
    return build_zip(fixture_dir(snapshot, product), compression)


def repackaged_zip():
    """Mismos archivos de venta 2026-10-07 con otro empaquetado, como el cambio de la fuente
    entre fotos: Deflated en vez de Stored, otro orden y con entrada de carpeta."""
    files = fixture_files()
    return make_zip({"metadatos/": b"", **dict(reversed(files.items()))}, zipfile.ZIP_DEFLATED)


def revised_zip():
    """Revisión silenciosa: cambia un valor del CSV de 2026 sin cambiar 'modified'."""
    files = fixture_files()
    files[VENTA_2026] = files[VENTA_2026].replace(b'"iX3",', b'"iX3 ",', 1)
    return make_zip(files)


def with_metadata(text_change):
    """Zip de venta 2026-10-07 con el metadato modificado por `text_change`."""
    files = fixture_files()
    files[VENTA_META] = text_change(files[VENTA_META].decode("utf-8")).encode("utf-8")
    return make_zip(files)


# --- lectura del metadato ---------------------------------------------------


@pytest.mark.parametrize("snapshot", SNAPSHOTS)
@pytest.mark.parametrize("product", PRODUCTS)
def test_product_and_date_come_from_the_zip_content(snapshot, product):
    info = read_snapshot_info(snapshot_zip(snapshot, product))
    assert info.product == product
    assert info.modified == date.fromisoformat(snapshot)


def test_metadata_fields():
    info = read_snapshot_info(snapshot_zip())
    assert info.temporal == "2005-01-01-2026-09-30"
    assert (info.temporal_start, info.temporal_end) == (date(2005, 1, 1), date(2026, 9, 30))
    assert info.metadata_name == VENTA_META
    assert info.metadata_bytes == fixture_files()[VENTA_META]


@pytest.mark.parametrize(
    ("compression", "expected"),
    [(zipfile.ZIP_STORED, "stored"), (zipfile.ZIP_DEFLATED, "deflated")],
)
def test_stored_and_deflated_zips_are_supported(compression, expected):
    assert read_snapshot_info(snapshot_zip(compression=compression)).compression == expected


def test_directory_entries_like_the_2026_09_09_zips_are_ignored():
    dirs = {"catalogos/": b"", "conjunto_de_datos/": b"", "metadatos/": b""}
    data = make_zip({**dirs, **fixture_files("2026-09-09")}, zipfile.ZIP_DEFLATED)
    info = read_snapshot_info(data)
    assert (info.product, info.modified) == ("venta", date(2026, 9, 9))
    assert info.compression == "deflated"


# --- zips que se rechazan ---------------------------------------------------


def test_not_a_zip_is_rejected():
    with pytest.raises(IngestError, match="no es un zip válido"):
        read_snapshot_info(b"<html>403 Forbidden</html>")


def test_corrupt_zip_is_rejected():
    data = snapshot_zip()
    i = data.index(b"Cifras Revisadas")  # en modo Stored los CSV van tal cual dentro del zip
    corrupt = data[:i] + b"Cifras Revisadaz" + data[i + 16 :]
    with pytest.raises(IngestError, match="CRC"):
        read_snapshot_info(corrupt)


def test_zip_without_metadata_is_rejected():
    files = {k: v for k, v in fixture_files().items() if not k.startswith("metadatos/")}
    with pytest.raises(IngestError, match="metadatos"):
        read_snapshot_info(make_zip(files))


def test_zip_without_data_csv_is_rejected():
    files = {k: v for k, v in fixture_files().items() if not k.startswith("conjunto_de_datos/")}
    with pytest.raises(IngestError, match="conjunto_de_datos"):
        read_snapshot_info(make_zip(files))


def test_metadata_and_data_of_different_products_are_rejected():
    files = {k: v for k, v in fixture_files().items() if not k.startswith("conjunto_de_datos/")}
    files.update(
        {k: v for k, v in fixture_files(product="produccion").items() if "conjunto" in k}
    )
    with pytest.raises(IngestError, match="produccion"):
        read_snapshot_info(make_zip(files))


def test_metadata_without_modified_is_rejected():
    with pytest.raises(IngestError, match="modified"):
        read_snapshot_info(with_metadata(lambda t: t.replace("modified: 2026-10-07", "")))


def test_invalid_modified_date_is_rejected():
    data = with_metadata(lambda t: t.replace("modified: 2026-10-07", "modified: 2026-13-40"))
    with pytest.raises(IngestError, match="modified"):
        read_snapshot_info(data)


def test_malformed_temporal_is_rejected():
    data = with_metadata(lambda t: t.replace("2005-01-01-2026-09-30", "2005-01-01"))
    with pytest.raises(IngestError, match="temporal"):
        read_snapshot_info(data)


# --- escritura en raw ------------------------------------------------------


@pytest.mark.parametrize("compression", [zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED])
def test_ingest_writes_zip_metadata_and_manifest(tmp_path, compression):
    storage = LocalStorage(str(tmp_path))
    data = snapshot_zip(compression=compression)
    result = ingest_zip(data, "venta.zip", storage, now=fixed_now)

    assert (result.status, result.product) == ("ingested", "venta")
    assert result.publication_date == date(2026, 10, 7)
    assert result.partition == VENTA_PARTITION == partition_path("venta", date(2026, 10, 7))
    assert storage.list(VENTA_PARTITION) == [
        VENTA_ZIP,
        VENTA_PARTITION + "manifest.json",
        VENTA_PARTITION + "metadatos_raiavl_venta_mensual_2005_2026.txt",
    ]
    assert storage.read_bytes(VENTA_ZIP) == data
    manifest = json.loads(storage.read_bytes(VENTA_PARTITION + "manifest.json"))
    assert manifest == {
        "product": "venta",
        "modified": "2026-10-07",
        "temporal": "2005-01-01-2026-09-30",
        "temporal_start": "2005-01-01",
        "temporal_end": "2026-09-30",
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
        "compression": "stored" if compression == zipfile.ZIP_STORED else "deflated",
        "ingested_at": "2026-10-07T18:30:00+00:00",
        "original_name": "venta.zip",
        "zip_name": "conjunto_de_datos_raiavl_mensual_venta_csv.zip",
        "metadata_name": "metadatos_raiavl_venta_mensual_2005_2026.txt",
        "files_sha256": {
            name: hashlib.sha256(content).hexdigest()
            for name, content in sorted(fixture_files().items())
        },
    }


def test_manifest_is_written_last():
    storage = MemoryStorage()
    ingest_zip(snapshot_zip(), "venta.zip", storage)
    assert storage.writes[-1] == VENTA_PARTITION + "manifest.json"
    assert len(storage.writes) == 3


def test_ingesting_twice_writes_nothing_the_second_time():
    storage = MemoryStorage()
    data = snapshot_zip()
    first = ingest_zip(data, "venta.zip", storage)
    writes = list(storage.writes)
    second = ingest_zip(data, "otro_nombre.zip", storage)
    assert (first.status, second.status) == ("ingested", "already_present")
    assert second.sha256 == first.sha256
    assert storage.writes == writes


def test_same_content_with_other_packaging_writes_nothing(caplog):
    storage = MemoryStorage()
    first = ingest_zip(snapshot_zip(), "venta.zip", storage)
    before = dict(storage.files)
    result = ingest_zip(repackaged_zip(), "venta.zip", storage)
    assert result.status == "same_content"
    assert result.sha256 != first.sha256
    assert storage.files == before
    assert any("mismo contenido con otro empaquetado" in m for m in caplog.messages)


def test_silent_revision_is_never_overwritten_and_names_the_changed_file():
    storage = MemoryStorage()
    ingest_zip(snapshot_zip(), "venta.zip", storage)
    before = dict(storage.files)
    expected = re.escape(f"(cambiaron: {VENTA_2026}); no se sobrescribe")
    with pytest.raises(SnapshotConflictError, match=expected):
        ingest_zip(revised_zip(), "venta.zip", storage)
    assert storage.files == before


def test_added_and_missing_files_are_reported():
    storage = MemoryStorage()
    ingest_zip(snapshot_zip(), "venta.zip", storage)
    files = fixture_files()
    files["leeme_faq.txt"] = b"preguntas frecuentes"
    del files["catalogos/tc_pais_origen.csv"]
    with pytest.raises(SnapshotConflictError) as exc:
        ingest_zip(make_zip(files), "venta.zip", storage)
    assert "nuevos: leeme_faq.txt" in str(exc.value)
    assert "faltan: catalogos/tc_pais_origen.csv" in str(exc.value)


def test_stored_manifest_without_file_hashes_is_a_conflict():
    storage = MemoryStorage()
    ingest_zip(snapshot_zip(), "venta.zip", storage)
    manifest = json.loads(storage.files[VENTA_PARTITION + "manifest.json"])
    del manifest["files_sha256"]
    storage.files[VENTA_PARTITION + "manifest.json"] = json.dumps(manifest).encode()
    with pytest.raises(SnapshotConflictError, match="no registra el sha256 de cada archivo"):
        ingest_zip(repackaged_zip(), "venta.zip", storage)


def test_partial_previous_write_is_completed():
    storage = MemoryStorage()
    storage.write_bytes(VENTA_ZIP, b"a medias")
    result = ingest_zip(snapshot_zip(), "venta.zip", storage)
    assert result.status == "ingested"
    assert storage.exists(VENTA_PARTITION + "manifest.json")
    assert storage.read_bytes(VENTA_ZIP) == snapshot_zip()


def test_rejected_zip_writes_nothing():
    storage = MemoryStorage()
    with pytest.raises(IngestError):
        ingest_zip(b"no es zip", "x.zip", storage)
    assert storage.writes == []


def test_two_snapshots_make_two_partitions_per_product():
    storage = MemoryStorage()
    for snapshot in SNAPSHOTS:
        for product in PRODUCTS:
            ingest_zip(snapshot_zip(snapshot, product), f"{product}.zip", storage)
    manifests = [p for p in storage.files if p.endswith("manifest.json")]
    assert len(manifests) == 8
    for product in PRODUCTS:
        assert sorted(p for p in manifests if p.startswith(f"raw/raiavl/{product}/")) == [
            f"raw/raiavl/{product}/publication_date=2026-09-09/manifest.json",
            f"raw/raiavl/{product}/publication_date=2026-10-07/manifest.json",
        ]


def test_renamed_zip_is_detected_by_content_and_keeps_only_the_file_name(tmp_path):
    path = tmp_path / "descargas" / "archivo (3).zip"
    path.parent.mkdir()
    path.write_bytes(snapshot_zip(product="hibrido"))
    storage = MemoryStorage()
    result = ingest_file(path, storage, now=fixed_now)
    assert result.product == "hibrido"
    manifest = json.loads(storage.read_bytes(result.partition + "manifest.json"))
    assert manifest["original_name"] == "archivo (3).zip"
    assert manifest["zip_name"] == "conjunto_de_datos_raiavl_mensual_hibrido_csv.zip"


def test_find_zips(tmp_path):
    (tmp_path / "b.zip").write_bytes(b"")
    (tmp_path / "a.zip").write_bytes(b"")
    (tmp_path / "notas.txt").write_bytes(b"")
    assert [p.name for p in find_zips(tmp_path)] == ["a.zip", "b.zip"]
    assert find_zips(tmp_path / "a.zip") == [tmp_path / "a.zip"]
    with pytest.raises(IngestError, match="no existe"):
        find_zips(tmp_path / "falta.zip")
    (tmp_path / "vacia").mkdir()
    with pytest.raises(IngestError, match="no hay archivos .zip"):
        find_zips(tmp_path / "vacia")


# --- CLI --------------------------------------------------------------------


@pytest.fixture
def samples(tmp_path, monkeypatch):
    """Carpeta con los 4 zips de la foto 2026-10-07 y el lake apuntando a tmp_path/lake."""
    folder = tmp_path / "samples"
    folder.mkdir()
    for product in PRODUCTS:
        name = f"conjunto_de_datos_raiavl_mensual_{product}_csv.zip"
        (folder / name).write_bytes(snapshot_zip(product=product))
    monkeypatch.setenv("INEGI_MARKET_BACKEND", "local")
    monkeypatch.setenv("INEGI_MARKET_DATA_DIR", str(tmp_path / "lake"))
    return folder


def test_cli_ingests_a_folder_and_is_idempotent(samples, tmp_path, caplog):
    caplog.set_level(logging.INFO)
    main(["ingest", "--source", "file", "--zip", str(samples)])
    lake = LocalStorage(str(tmp_path / "lake"))
    assert len([p for p in lake.list("raw/raiavl/") if p.endswith("manifest.json")]) == 4

    caplog.clear()
    main(["ingest", "--zip", str(samples)])
    assert sum("ya existe con el mismo sha256" in m for m in caplog.messages) == 4


def test_cli_conflict_exits_with_error(samples, tmp_path, caplog):
    main(["ingest", "--zip", str(samples / "conjunto_de_datos_raiavl_mensual_venta_csv.zip")])
    other = tmp_path / "venta_revisada.zip"
    other.write_bytes(revised_zip())
    with pytest.raises(SystemExit) as exc:
        main(["ingest", "--zip", str(other)])
    assert exc.value.code == 1
    assert any("no se sobrescribe" in m for m in caplog.messages)


def test_cli_repackaged_zip_is_not_an_error(samples, tmp_path, caplog):
    main(["ingest", "--zip", str(samples / "conjunto_de_datos_raiavl_mensual_venta_csv.zip")])
    other = tmp_path / "venta_reempaquetada.zip"
    other.write_bytes(repackaged_zip())
    main(["ingest", "--zip", str(other)])  # no lanza SystemExit: termina con código 0
    assert any("mismo contenido con otro empaquetado" in m for m in caplog.messages)


@pytest.mark.parametrize(
    "argv", [["ingest"], ["ingest", "--source", "http", "--zip", "x.zip"]], ids=["sin-zip", "http"]
)
def test_cli_rejects_incomplete_or_unknown_arguments(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2
