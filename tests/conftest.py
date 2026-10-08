import io
import zipfile
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "raiavl"
SNAPSHOTS = ("2026-09-09", "2026-10-07")
PRODUCTS = ("venta", "produccion", "exportacion", "hibrido")


def fixture_dir(snapshot: str, product: str) -> Path:
    return FIXTURES / snapshot / product


def tree_files(src_dir: Path) -> dict[str, bytes]:
    """Contenido de una carpeta como {ruta dentro del zip: bytes}."""
    return {
        p.relative_to(src_dir).as_posix(): p.read_bytes()
        for p in sorted(src_dir.rglob("*"))
        if p.is_file()
    }


def fixture_files(snapshot: str = "2026-10-07", product: str = "venta") -> dict[str, bytes]:
    return tree_files(fixture_dir(snapshot, product))


def make_zip(files: dict[str, bytes], compression: int = zipfile.ZIP_STORED) -> bytes:
    """Arma un zip en memoria, determinista (fecha y orden fijos).

    Las rutas que terminan en "/" son entradas de directorio, como las que traían
    los zips del INEGI del 2026-09-09; se guardan sin comprimir, igual que en la fuente.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, (2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED if name.endswith("/") else compression
            zf.writestr(info, data)
    return buf.getvalue()


def build_zip(src_dir: Path, compression: int = zipfile.ZIP_STORED) -> bytes:
    """Zip con la estructura de `src_dir`, como los del INEGI.

    Los zips no se versionan (`*.zip` está en .gitignore), así que las pruebas
    los construyen a partir de los fixtures.
    """
    return make_zip(tree_files(src_dir), compression)


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
