import pytest

from inegi_market.config import Settings
from inegi_market.storage import GCSStorage, LocalStorage, get_storage


def test_local_write_read_exists(tmp_path):
    storage = LocalStorage(str(tmp_path))
    storage.write_bytes("raw/a/b.bin", b"hola")
    assert storage.exists("raw/a/b.bin")
    assert storage.read_bytes("raw/a/b.bin") == b"hola"
    assert not storage.exists("raw/a/otro.bin")


def test_local_write_replaces_and_leaves_no_tmp(tmp_path):
    storage = LocalStorage(str(tmp_path))
    storage.write_bytes("x/f.json", b"1")
    storage.write_bytes("x/f.json", b"2")
    assert storage.read_bytes("x/f.json") == b"2"
    assert [p.name for p in (tmp_path / "x").iterdir()] == ["f.json"]


def test_local_list_ignores_tmp_files(tmp_path):
    storage = LocalStorage(str(tmp_path))
    storage.write_bytes("raw/a.zip", b"z")
    (tmp_path / "raw" / "b.zip.tmp").write_bytes(b"a medias")
    assert storage.list("raw/") == ["raw/a.zip"]


def test_local_list_with_partial_prefix_matches_gcs_semantics(tmp_path):
    storage = LocalStorage(str(tmp_path))
    for path in ("raw/venta/p=1/a", "raw/venta/p=2/a", "raw/ventas_x/a", "raw/hibrido/p=1/a"):
        storage.write_bytes(path, b"")
    assert storage.list("raw/venta/") == ["raw/venta/p=1/a", "raw/venta/p=2/a"]
    assert storage.list("raw/venta") == ["raw/venta/p=1/a", "raw/venta/p=2/a", "raw/ventas_x/a"]
    assert storage.list("raw/nada/") == []
    assert len(storage.list("")) == 4


class FakeBlob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def upload_from_string(self, data):
        self.store[self.name] = data

    def download_as_bytes(self):
        return self.store[self.name]

    def exists(self):
        return self.name in self.store


class FakeBucket:
    def __init__(self):
        self.store = {}

    def blob(self, name):
        return FakeBlob(self.store, name)

    def list_blobs(self, prefix):
        return [FakeBlob(self.store, n) for n in self.store if n.startswith(prefix)]


class FakeClient:
    def __init__(self):
        self.buckets = {}

    def bucket(self, name):
        return self.buckets.setdefault(name, FakeBucket())


def test_gcs_storage_with_injected_client():
    client = FakeClient()
    storage = GCSStorage("lake", client=client)
    storage.write_bytes("raw/b.zip", b"b")
    storage.write_bytes("raw/a.zip", b"a")
    assert storage.read_bytes("raw/a.zip") == b"a"
    assert storage.exists("raw/b.zip") and not storage.exists("raw/c.zip")
    assert storage.list("raw/") == ["raw/a.zip", "raw/b.zip"]
    assert set(client.buckets) == {"lake"}


def test_get_storage_local(tmp_path):
    storage = get_storage(Settings(data_dir=str(tmp_path)))
    assert isinstance(storage, LocalStorage)


def test_get_storage_gcs_requires_bucket():
    with pytest.raises(ValueError, match="INEGI_MARKET_BUCKET"):
        get_storage(Settings(backend="gcs"))


def test_get_storage_rejects_unknown_backend():
    with pytest.raises(ValueError, match="INEGI_MARKET_BACKEND"):
        get_storage(Settings(backend="s3"))
