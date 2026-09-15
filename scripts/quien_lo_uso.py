#!/usr/bin/env python
"""Quien ha usado el agente, y desde donde.

    python scripts/quien_lo_uso.py           # ultimos 7 dias
    python scripts/quien_lo_uso.py --dias 30

Lee dos fuentes y las junta:

  - Cloud Logging, que recibe el JSON de cada turno por stdout. Siempre activo,
    con retencion de 30 dias por defecto.
  - Las cabeceras HTTP, para distinguir quien llamo: la plataforma usa Bun,
    las pruebas manuales usan curl.

Lo mismo se puede consultar con SQL sobre la vista cv_agent.turnos, que une lo
que escribio el proceso con lo que escribe el sink de Cloud Logging:

    bq query --use_legacy_sql=false       'SELECT categoria, COUNT(*) FROM 
        WHERE marca_tiempo >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 7 DAY)
        GROUP BY categoria ORDER BY 2 DESC'

NO muestra lo que se hablo, porque no se guarda. Ni las preguntas ni las
respuestas salen del proceso: solo la categoria, que se clasifica en memoria.
Esa es una decision de diseno, no una limitacion de esta herramienta.
"""

from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys

PROYECTO = "cv-agent-edher"
SERVICIO = "cv-agent"

# Como se reconoce quien llama, por su cabecera de agente de usuario.
ORIGENES = [
    ("Bun", "la plataforma (Parley)"),
    ("curl", "prueba manual con curl"),
    ("Python-urllib", "prueba desde codigo"),
    ("python-requests", "prueba desde codigo"),
    ("Mozilla", "alguien con un navegador"),
    ("node", "un cliente de Node"),
]


def _leer_logs(filtro: str, dias: int, limite: int = 500) -> list[dict]:
    salida = subprocess.run(
        ["gcloud", "logging", "read", filtro, "--project", PROYECTO,
         f"--freshness={dias}d", f"--limit={limite}", "--format=json"],
        capture_output=True, text=True, shell=(sys.platform == "win32"),
    )
    if salida.returncode != 0:
        print("No se pudo leer Cloud Logging. ¿Esta autenticado gcloud?", file=sys.stderr)
        print(salida.stderr.strip()[:400], file=sys.stderr)
        raise SystemExit(1)
    try:
        return json.loads(salida.stdout or "[]")
    except json.JSONDecodeError:
        return []


def _clasificar(agente: str) -> str:
    for marca, nombre in ORIGENES:
        if marca.lower() in (agente or "").lower():
            return nombre
    return agente or "desconocido"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=7)
    args = ap.parse_args()

    base = f'resource.type=cloud_run_revision AND resource.labels.service_name={SERVICIO}'

    turnos = [
        e["jsonPayload"]
        for e in _leer_logs(f'{base} AND jsonPayload.evento="turno_completado"', args.dias)
    ]
    peticiones = _leer_logs(f'{base} AND httpRequest.requestUrl:"/v1/responses"', args.dias)

    print(f"\n  Ultimos {args.dias} dias\n" + "  " + "-" * 62)

    if not turnos:
        print("  Nadie ha hablado con el agente en este periodo.\n")
        return

    origenes = collections.Counter(
        _clasificar(p.get("httpRequest", {}).get("userAgent", "")) for p in peticiones
    )
    print("\n  QUIEN LLAMO")
    for nombre, n in origenes.most_common():
        print(f"    {n:>4}  {nombre}")

    dias = collections.Counter(t["marca_tiempo"][:10] for t in turnos)
    print("\n  ACTIVIDAD POR DIA")
    for d in sorted(dias):
        print(f"    {d}   {dias[d]:>3} turnos")

    cats = collections.Counter(t.get("categoria") or "sin_clasificar" for t in turnos)
    print("\n  DE QUE PREGUNTARON")
    for c, n in cats.most_common(10):
        print(f"    {n:>4}  {c}")

    convs = {t["id_conversacion"] for t in turnos}
    con_citas = sum(1 for t in turnos if t.get("fundamentado"))
    etiquetas = collections.Counter(
        e for t in turnos for e in (t.get("etiquetas_guardrail") or [])
    )
    lat = sorted(t["latencia_ms"] for t in turnos if t.get("latencia_ms"))

    print("\n  RESUMEN")
    print(f"    turnos                 {len(turnos)}")
    print(f"    conversaciones         {len(convs)}")
    print(f"    con citas del CV       {con_citas} de {len(turnos)}")
    if lat:
        print(f"    latencia mediana       {lat[len(lat)//2]/1000:.1f} s")
    if etiquetas:
        print("\n  GUARDRAILS QUE DISPARARON")
        for e, n in etiquetas.most_common():
            print(f"    {n:>4}  {e}")

    print("\n  El texto de las conversaciones no se guarda, por diseno.\n")


if __name__ == "__main__":
    main()
