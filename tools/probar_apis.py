"""Prueba de las APIs del proyecto 2 (Fase 0). Solo lectura, sin dependencias externas.

Uso (PowerShell):
    $env:INEGI_TOKEN   = "<tu token del INEGI>"
    $env:BANXICO_TOKEN = "<tu token de Banxico>"
    python probar_apis.py

Los tokens se leen de variables de entorno y nunca se imprimen.
"""
import json
import os
import sys
import urllib.error
import urllib.request

INEGI_BASE = (
    "https://www.inegi.org.mx/app/api/indicadores/desarrolladores/jsonxml/"
    "INDICATOR/{ind}/es/00/{recent}/{fuente}/2.0/{token}?type=json"
)
BANXICO_BASE = "https://www.banxico.org.mx/SieAPIRest/service/v1/series/{serie}/datos/{tramo}"

INDICADORES = {
    "6207131346": "Ventas totales (unidades)",
    "6207131345": "Unidades producidas",
    "6207131349": "Unidades exportadas",
    "6207131348": "Variación de ventas totales (%)",
    "6207131347": "Variación de exportadas (%)",
    "6207131344": "Variación de producidas (%)",
}

# Totales de referencia calculados a partir de los CSV descargados el 2026-10-06.
# Producción y exportación de los últimos meses pueden ser preliminares.
SUMA_CSV = {
    "6207131346": {"2026/08": 129362, "2026/07": 130835},
    "6207131345": {"2026/08": 344940, "2026/07": 302984},
    "6207131349": {"2026/08": 300475, "2026/07": 261534},
}


def http_get(url, headers=None, secret=""):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "inegi-auto-market/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        cuerpo = e.read().decode("utf-8", "replace")[:300]
        return e.code, cuerpo.replace(secret, "***") if secret else cuerpo
    except Exception as e:  # red, DNS, timeout
        msg = str(e).replace(secret, "***") if secret else str(e)
        return None, msg


def inegi(ind, token, recent="false"):
    """Devuelve (fuente, observaciones) probando BISE y luego BIE."""
    for fuente in ("BISE", "BIE"):
        url = INEGI_BASE.format(ind=ind, recent=recent, fuente=fuente, token=token)
        status, body = http_get(url, secret=token)
        if status != 200:
            print(f"   {fuente}: HTTP {status} {body[:120]}")
            continue
        try:
            obs = json.loads(body)["Series"][0]["OBSERVATIONS"]
        except (ValueError, KeyError, IndexError, TypeError):
            print(f"   {fuente}: respuesta sin 'Series/OBSERVATIONS': {body[:160]}")
            continue
        if obs:
            return fuente, obs
    return None, []


def probar_inegi(token):
    print("== INEGI: Banco de Indicadores ==")
    ok = True
    for ind, nombre in INDICADORES.items():
        fuente, obs = inegi(ind, token)
        if not obs:
            print(f"[FALLA] {ind} {nombre}: sin datos")
            ok = False
            continue
        ult = sorted(obs, key=lambda o: o["TIME_PERIOD"])[-3:]
        print(f"[OK] {ind} {nombre} | fuente {fuente} | {len(obs)} observaciones "
              f"({min(o['TIME_PERIOD'] for o in obs)} a {max(o['TIME_PERIOD'] for o in obs)})")
        for o in ult:
            print(f"      {o['TIME_PERIOD']}: {o['OBS_VALUE']}  estatus={o.get('OBS_STATUS')}")
        ref = SUMA_CSV.get(ind)
        if ref:
            por_periodo = {o["TIME_PERIOD"]: o["OBS_VALUE"] for o in obs}
            for periodo, suma in ref.items():
                api = por_periodo.get(periodo)
                try:
                    dif = float(api) - suma
                    marca = "CUADRA" if dif == 0 else f"DIFIERE ({dif:+,.0f})"
                except (TypeError, ValueError):
                    marca = f"periodo {periodo} no encontrado en la API"
                print(f"      conciliación {periodo}: API={api} vs suma CSV={suma:,} -> {marca}")
    return ok


def probar_banxico(token):
    print("\n== Banxico SIE: tipo de cambio FIX (SF43718) ==")
    h = {"Bmx-Token": token, "Accept": "application/json"}
    ok = True
    for tramo in ("oportuno", "2026-09-01/2026-10-06"):
        status, body = http_get(BANXICO_BASE.format(serie="SF43718", tramo=tramo), h, secret=token)
        if status != 200:
            print(f"[FALLA] {tramo}: HTTP {status} {body[:160]}")
            ok = False
            continue
        try:
            serie = json.loads(body)["bmx"]["series"][0]
            datos = serie["datos"]
            print(f"[OK] {tramo} | {serie.get('titulo', '')[:70]} | {len(datos)} datos")
            for d in datos[-3:]:
                print(f"      {d['fecha']}: {d['dato']}")
        except (ValueError, KeyError, IndexError, TypeError):
            print(f"[FALLA] {tramo}: estructura inesperada: {body[:160]}")
            ok = False
    return ok


def main():
    inegi_t, bmx_t = os.environ.get("INEGI_TOKEN", ""), os.environ.get("BANXICO_TOKEN", "")
    if not inegi_t or not bmx_t:
        sys.exit("Define INEGI_TOKEN y BANXICO_TOKEN como variables de entorno.")
    a = probar_inegi(inegi_t)
    b = probar_banxico(bmx_t)
    resultado = "todo bien" if a and b else "hay fallas; pégame la salida (no contiene tokens)"
    print("\nResultado:", resultado)


if __name__ == "__main__":
    main()
