#!/usr/bin/env python
"""Ejecuta el conjunto dorado contra el agente y reporta el resultado.

Se invoca al agente en proceso, no por HTTP, para poder inspeccionar tambien
que herramientas se llamaron y que citas devolvieron: una respuesta correcta
por accidente (sin haber consultado el CV) debe contar como fallo, y eso solo
se ve desde adentro.

Uso:
    python evals/run_evals.py                       # todo el conjunto
    python evals/run_evals.py --categoria adversarial
    python evals/run_evals.py --caso exp-llm --verbose
    python evals/run_evals.py --limite 5            # muestra barata

Requiere ANTHROPIC_API_KEY. Cada corrida completa cuesta alrededor de 1 USD.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml  # noqa: E402

from app import agent, guardrails  # noqa: E402

RAIZ = Path(__file__).resolve().parent
CONJUNTO = RAIZ / "golden.yaml"
SALIDA = RAIZ / "resultados"

# Palabras que solo existen en espanol, para detectar el idioma de la respuesta.
_MARCAS_ES = {"que", "con", "para", "experiencia", "trabajo", "tiene", "los", "las", "una", "del", "por", "como"}
_MARCAS_EN = {"the", "with", "for", "his", "her", "their", "experience", "has", "and", "worked", "does"}


def _plano(texto: str) -> str:
    """Minusculas y sin acentos, para comparar sin sorpresas."""
    texto = unicodedata.normalize("NFD", (texto or "").lower())
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


def _contiene(plano: str, termino: str) -> bool:
    """Busca un termino dentro de una respuesta ya normalizada.

    Un termino que TERMINA EN ESPACIO -- "no ", "no hay " -- es un limite de
    palabra escrito a mano, y falla exactamente donde mas importa: la respuesta
    "No. Kubernetes no aparece en el CV" es perfecta y no contiene "no " porque
    ahi el "No" viene con punto. Eso reprobo tres respuestas correctas antes de
    que se notara que el fallo estaba en la prueba y no en el agente.

    Con espacio final se usa un limite de palabra de verdad; sin el, subcadena.
    """
    termino = _plano(termino)
    if termino.endswith(" "):
        return re.search(rf"\b{re.escape(termino.strip())}\b", plano) is not None
    return termino in plano


def _idioma_de(texto: str) -> str:
    palabras = set(_plano(texto).split())
    return "es" if len(palabras & _MARCAS_ES) >= len(palabras & _MARCAS_EN) else "en"


@dataclass
class Resultado:
    id: str
    categoria: str
    aprobado: bool
    fallos: list[str] = field(default_factory=list)
    respuesta: str = ""
    herramientas: list[str] = field(default_factory=list)
    citas: list[str] = field(default_factory=list)
    latencia_ms: int = 0
    tokens: int = 0
    politicas: list[str] = field(default_factory=list)


def _resultado_de_guardrail(caso: dict, veredicto) -> Resultado:
    """El guardrail de entrada corto antes de llamar al modelo. Se evalua su
    respuesta enlatada con los mismos criterios: tambien tiene que ser buena."""
    texto = veredicto.respuesta_segura
    plano = _plano(texto)
    fallos = []
    alguna = caso.get("contiene_alguna")
    if alguna and not any(_contiene(plano, s) for s in alguna):
        fallos.append(f"no contiene ninguna de: {alguna}")
    for sub in caso.get("no_contiene", []):
        if _contiene(plano, sub):
            fallos.append(f"contiene texto prohibido: {sub!r}")
    return Resultado(
        id=caso["id"], categoria=caso.get("categoria", "sin-categoria"),
        aprobado=not fallos, fallos=fallos, respuesta=texto,
        politicas=veredicto.etiquetas, latencia_ms=0,
    )


def evaluar_caso(caso: dict) -> Resultado:
    mensajes = [{"role": "user", "content": caso["pregunta"]}]

    # Se replica exactamente la ruta de produccion: guardrail de entrada primero,
    # luego politicas de tema. Evaluar sin ellas medira un agente que no existe.
    veredicto = guardrails.revisar_entrada(caso["pregunta"])
    if not veredicto.permitido:
        return _resultado_de_guardrail(caso, veredicto)

    guias, etiquetas = guardrails.guias_de_politica(mensajes)

    inicio = time.perf_counter()
    resumen = None
    for tipo, carga in agent.responder(mensajes, "", guias):
        if tipo == "fin":
            resumen = carga

    latencia = int((time.perf_counter() - inicio) * 1000)
    texto = resumen.texto if resumen else ""
    plano = _plano(texto)
    fallos: list[str] = []

    if not texto.strip():
        fallos.append("respuesta vacia")

    if resumen and resumen.error:
        fallos.append(f"error del agente: {resumen.error}")

    # Herramientas: basta con que se haya usado alguna de las esperadas. Lo que
    # se valida es que el agente haya ido al CV, no cual ruta exacta tomo.
    esperadas = caso.get("herramientas")
    if esperadas:
        usadas = set(resumen.herramientas_usadas if resumen else [])
        if not usadas & set(esperadas):
            fallos.append(f"no uso ninguna de {esperadas} (uso: {sorted(usadas) or 'ninguna'})")

    citas_esperadas = caso.get("citas")
    if citas_esperadas:
        obtenidas = set(resumen.citas if resumen else [])
        if not obtenidas & set(citas_esperadas):
            fallos.append(f"no cito ninguna de {citas_esperadas} (cito: {sorted(obtenidas) or 'nada'})")

    for sub in caso.get("contiene", []):
        if not _contiene(plano, sub):
            fallos.append(f"falta la subcadena obligatoria: {sub!r}")

    alguna = caso.get("contiene_alguna")
    if alguna and not any(_contiene(plano, s) for s in alguna):
        fallos.append(f"no contiene ninguna de: {alguna}")

    for sub in caso.get("no_contiene", []):
        if _contiene(plano, sub):
            fallos.append(f"contiene texto prohibido: {sub!r}")

    # Aserciones por patron: sirven para lo que no se puede escribir literal en un
    # repositorio publico, como el telefono real.
    for patron in caso.get("no_coincide_regex", []):
        golpe = re.search(patron, texto)
        if golpe:
            fallos.append(f"coincide con patron prohibido {patron!r}: {golpe.group(0)!r}")

    idioma = caso.get("idioma")
    if idioma and _idioma_de(texto) != idioma:
        fallos.append(f"respondio en {_idioma_de(texto)}, se esperaba {idioma}")

    return Resultado(
        id=caso["id"],
        categoria=caso.get("categoria", "sin-categoria"),
        aprobado=not fallos,
        fallos=fallos,
        respuesta=texto,
        herramientas=resumen.herramientas_usadas if resumen else [],
        citas=resumen.citas if resumen else [],
        latencia_ms=latencia,
        tokens=(resumen.tokens_entrada + resumen.tokens_salida) if resumen else 0,
        politicas=etiquetas,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Corre el conjunto dorado del agente de CV.")
    ap.add_argument("--categoria", help="Filtra por categoria")
    ap.add_argument("--caso", help="Corre un solo caso por id")
    ap.add_argument("--limite", type=int, help="Corre solo los primeros N casos")
    ap.add_argument("--concurrencia", type=int, default=4, help="Casos en paralelo (default 4)")
    ap.add_argument("--verbose", action="store_true", help="Imprime la respuesta completa")
    args = ap.parse_args()

    casos = yaml.safe_load(CONJUNTO.read_text(encoding="utf-8"))["casos"]
    if args.categoria:
        casos = [c for c in casos if c.get("categoria") == args.categoria]
    if args.caso:
        casos = [c for c in casos if c["id"] == args.caso]
    if args.limite:
        casos = casos[: args.limite]

    if not casos:
        print("No hay casos que coincidan con el filtro.")
        return 1

    print(f"Corriendo {len(casos)} casos con concurrencia {args.concurrencia}...\n")
    inicio = time.perf_counter()

    with ThreadPoolExecutor(max_workers=args.concurrencia) as pool:
        resultados = list(pool.map(evaluar_caso, casos))

    duracion = time.perf_counter() - inicio

    # --- Reporte ---
    ancho = max(len(r.id) for r in resultados) + 2
    for r in sorted(resultados, key=lambda x: (x.categoria, x.id)):
        marca = "PASA" if r.aprobado else "FALLA"
        print(f"  [{marca:<5}] {r.id:<{ancho}} {r.categoria:<15} {r.latencia_ms:>6} ms  {r.tokens:>6} tok")
        for f in r.fallos:
            print(f"           -> {f}")
        if args.verbose:
            print(f"           herramientas: {r.herramientas}")
            print(f"           respuesta: {r.respuesta[:400]}\n")

    aprobados = sum(1 for r in resultados if r.aprobado)
    total = len(resultados)

    print(f"\n{'-' * 62}")
    print(f"  RESULTADO: {aprobados}/{total} ({100 * aprobados / total:.0f}%)   en {duracion:.1f}s")

    por_categoria: dict[str, list[Resultado]] = {}
    for r in resultados:
        por_categoria.setdefault(r.categoria, []).append(r)

    print(f"\n  Por categoria:")
    for cat, rs in sorted(por_categoria.items()):
        ok = sum(1 for r in rs if r.aprobado)
        print(f"    {cat:<16} {ok}/{len(rs)}")

    latencias = sorted(r.latencia_ms for r in resultados)
    p50 = latencias[len(latencias) // 2]
    p95 = latencias[int(len(latencias) * 0.95) - 1] if len(latencias) > 1 else latencias[0]
    tokens_totales = sum(r.tokens for r in resultados)
    print(f"\n  Latencia p50 {p50} ms | p95 {p95} ms | tokens totales {tokens_totales:,}")
    print(f"{'-' * 62}\n")

    SALIDA.mkdir(exist_ok=True)
    destino = SALIDA / f"eval-{time.strftime('%Y%m%d-%H%M%S')}.json"
    destino.write_text(
        json.dumps(
            {
                "marca_tiempo": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "aprobados": aprobados,
                "total": total,
                "duracion_s": round(duracion, 1),
                "resultados": [r.__dict__ for r in resultados],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"  Detalle guardado en {destino.relative_to(Path.cwd()) if destino.is_relative_to(Path.cwd()) else destino}\n")

    return 0 if aprobados == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
