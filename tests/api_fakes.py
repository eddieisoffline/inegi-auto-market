"""Dobles de las APIs del INEGI y de Banxico: respuestas con la estructura documentada."""
import json

from inegi_market.sources.http_client import HttpResponse

INEGI_TOKEN = "tok-inegi-0000"      # tokens de prueba: no son reales
BANXICO_TOKEN = "tok-banxico-0000"


def inegi_body(values, indicator="6207131346", status="2"):
    """values: {(año, mes): OBS_VALUE en texto}."""
    observations = [
        {"TIME_PERIOD": f"{y}/{m:02d}", "OBS_VALUE": v, "OBS_EXCEPTION": None,
         "OBS_STATUS": status, "OBS_SOURCE": "BISE", "OBS_NOTE": None, "COBER_GEO": "0700"}
        for (y, m), v in sorted(values.items())
    ]
    return json.dumps({"Header": {"Name": "INEGI"},
                       "Series": [{"INDICADOR": indicator, "OBSERVATIONS": observations}]}).encode()


def banxico_body(values):
    """values: [(fecha "dd/mm/aaaa", dato en texto)]."""
    datos = [{"fecha": f, "dato": d} for f, d in values]
    titulo = "Tipo de cambio Pesos por dólar E.U.A. Para solventar obligaciones    "
    return json.dumps({"bmx": {"series": [{"idSerie": "SF43718", "titulo": titulo,
                                           "datos": datos}]}}).encode()


class FakeApi:
    """Responde según un fragmento de la URL. Cada ruta es una lista: se consume en orden y
    el último elemento se repite. Un elemento es (status, cuerpo) o una excepción."""

    def __init__(self, routes):
        self.routes = {k: list(v) for k, v in routes.items()}
        self.calls = []

    def head(self, url, headers):
        raise AssertionError("las APIs no usan HEAD")

    def get(self, url, headers, dest):
        self.calls.append((url, dict(headers)))
        key = next(k for k in self.routes if k in url)
        items = self.routes[key]
        item = items.pop(0) if len(items) > 1 else items[0]
        if isinstance(item, Exception):
            raise item
        status, body = item
        dest.write(body)
        return HttpResponse(status, {"content-type": "application/json"})
