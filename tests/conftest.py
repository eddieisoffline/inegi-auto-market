import io
import zipfile
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "raiavl"
SNAPSHOTS = ("2026-09-09", "2026-10-07")
PRODUCTS = ("venta", "produccion", "exportacion", "hibrido")


def fixture_dir(snapshot: str, product: str) -> Path:
    return FIXTURES / snapshot / product


def build_zip(src_dir: Path, compression: int = zipfile.ZIP_STORED) -> bytes:
    """Arma en memoria un zip con la estructura de `src_dir`, como los del INEGI.

    Los zips no se versionan (`*.zip` está en .gitignore), así que las pruebas
    los construyen a partir de los fixtures. Es determinista: fecha y orden
    fijos, para que el mismo árbol produzca siempre los mismos bytes.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path in sorted(p for p in src_dir.rglob("*") if p.is_file()):
            info = zipfile.ZipInfo(path.relative_to(src_dir).as_posix(), (2026, 1, 1, 0, 0, 0))
            info.compress_type = compression
            zf.writestr(info, path.read_bytes())
    return buf.getvalue()
