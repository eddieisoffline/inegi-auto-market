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


CRLF = "\r\n"  # fin de línea de los CSV del INEGI
HYBRID_HEADER = (
    '"PROD_EST","COBERTURA","ANIO","ID_MES","ID_ENTIDAD","VEH_ELECTR","VEH_HIBRIDAS_PLUGIN",'
    '"VEH_HIBRIDAS","ESTATUS"'
)
HYBRID_PROD_EST = (
    "Registro Administrativo de la Industria Automotriz de Vehículos Ligeros. "
    "Venta de vehículos híbridos y eléctricos"
)


def complete_hybrid_files(end=(2026, 9), entities=("01", "09"), skip=()):
    """Foto de híbridos completa, de 2016-01 al mes `end`, con cifras sintéticas.

    Los fixtures son una muestra de meses, así que no sirven para probar la continuidad.
    Esta foto usa el metadato y los catálogos reales del fixture 2026-10-07 y genera un CSV
    por año; los estatus siguen la ventana de 36 meses observada en las fotos reales.
    `skip` quita meses (año, mes) para provocar huecos.
    """
    files = {k: v for k, v in fixture_files("2026-10-07", "hibrido").items()
             if not k.startswith("conjunto_de_datos/")}
    last = end[0] * 12 + end[1]
    for year in range(2016, end[0] + 1):
        lines = [HYBRID_HEADER]
        for month in range(1, 13):
            if year * 12 + month > last or (year, month) in skip:
                continue
            age = last - (year * 12 + month)  # meses antes del último publicado
            status = "Cifras Definitivas" if age >= 36 else "Cifras Revisadas"
            for entity in entities:
                lines.append(f'"{HYBRID_PROD_EST}","Nacional",{year},"{month:02d}","{entity}",'
                             f'1,2,3,"{status}"')
        name = f"conjunto_de_datos/raiavl_hibrido_mensual_tr_cifra_{year}.csv"
        files[name] = (CRLF.join(lines) + CRLF).encode("utf-8")
    return files
