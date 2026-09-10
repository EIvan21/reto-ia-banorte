#!/usr/bin/env python
"""Mide el corpus y la calidad de recuperacion, para decidir con datos.

La pregunta que responde: ¿la busqueda lexica sigue siendo suficiente, o ya toca
recuperacion semantica?

El banco de pruebas separa dos tipos de consulta a proposito:

  LEXICAS    comparten vocabulario con el CV. Es el caso facil, y donde la
             busqueda por palabra clave deberia ir bien.
  SEMANTICAS preguntan lo mismo con otras palabras, como habla una persona que
             no ha leido el CV. Es el caso que un indice vectorial resuelve y
             uno lexico no puede: no hay palabras que empatar.

Si la brecha entre ambas es chica, el enfoque actual aguanta. Si es grande, los
vectores se ganaron su lugar. Eso es lo que decide, no la opinion.

    python evals/medir_corpus.py            # sin llamar a la API
    python evals/medir_corpus.py --tokens   # cuenta tokens reales con la API
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import tools  # noqa: E402
from app.config import load_cv  # noqa: E402

# --- Banco de pruebas -------------------------------------------------------
# Cada caso: (pregunta, ids aceptables). Se acepta cualquiera de la lista porque
# varias entradas pueden responder legitimamente la misma pregunta.

LEXICAS: list[tuple[str, set[str]]] = [
    ("¿Qué experiencia tiene con LookML?", {"hab-bi", "exp-globallogic", "proy-looker-architect"}),
    ("¿Ha trabajado con BigQuery?", {"hab-cloud", "exp-gtec", "exp-globallogic"}),
    ("¿Qué hizo en Infosys?", {"exp-infosys"}),
    ("¿Tiene certificaciones de Google Cloud?", {"cert-ace", "cert-genai", "hab-cloud"}),
    ("¿Qué contribuciones open source tiene?", {"open-source", "proy-ga-four", "proy-agent-analytics"}),
    ("¿Cuál es el Agent Analytics Block?", {"proy-agent-analytics"}),
    ("¿Qué estudió en la universidad?", {"edu-licenciatura", "trayectoria"}),
    ("¿Toca la guitarra?", {"intereses"}),
    ("¿Qué tipo de rol busca?", {"preferencias-rol"}),
    ("¿Tiene experiencia con Dataflow?", {"hab-cloud", "exp-gtec"}),
    ("¿Qué hace en GlobalLogic?", {"exp-globallogic"}),
    ("¿Ha publicado algún artículo?", {"publicaciones"}),
]

SEMANTICAS: list[tuple[str, set[str]]] = [
    # Misma pregunta, vocabulario de alguien que no ha leido el CV.
    ("¿Ha tenido que tratar con gente enfadada en el trabajo?", {"exp-infosys", "forma-de-trabajar"}),
    ("¿Sabe explicarle cosas complicadas a alguien que no es del área?", {"forma-de-trabajar", "exp-globallogic"}),
    ("¿Le ha ahorrado dinero a alguna empresa?", {"exp-gtec", "exp-globallogic"}),
    ("¿Ha arreglado algo que iba muy despacio?", {"proy-looker-performance", "exp-infosys"}),
    ("¿Es de los que aprenden por su cuenta?", {"trayectoria", "forma-de-trabajar"}),
    ("¿Puede trabajar con gente de otros husos horarios?", {"forma-de-trabajar", "exp-globallogic"}),
    ("¿Ha creado algo que otras personas usen?", {"open-source", "forma-de-trabajar", "proy-looker-architect"}),
    ("¿Cómo reacciona cuando no sabe algo?", {"forma-de-trabajar"}),
    ("¿Tiene algo que lo haga distinto de otros candidatos?", {"preferencias-rol", "forma-de-trabajar", "open-source"}),
    ("¿Ha coordinado a otras personas?", {"exp-infosys", "forma-de-trabajar"}),
    ("¿Qué lo mueve, más allá del sueldo?", {"trayectoria", "preferencias-rol"}),
    ("¿Sabe montar algo desde cero y dejarlo funcionando?", {"exp-gtec", "proy-cv-agent", "proy-video-multiagente"}),
]


def _recuperar(pregunta: str, k: int) -> list[str]:
    resultado = tools.buscar_cv(pregunta, "")
    return resultado.get("_citas", [])[:k]


def _evaluar(banco: list[tuple[str, set[str]]], k: int) -> tuple[float, list[str]]:
    """Recall@k: en cuantas preguntas aparece al menos una entrada correcta."""
    aciertos, fallos = 0, []
    for pregunta, esperados in banco:
        if set(_recuperar(pregunta, k)) & esperados:
            aciertos += 1
        else:
            fallos.append(pregunta)
    return aciertos / len(banco), fallos


def _rango_reciproco(banco: list[tuple[str, set[str]]]) -> float:
    """MRR: que tan arriba aparece el primer resultado correcto."""
    total = 0.0
    for pregunta, esperados in banco:
        for i, cita in enumerate(_recuperar(pregunta, 8), start=1):
            if cita in esperados:
                total += 1 / i
                break
    return total / len(banco)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tokens", action="store_true", help="Cuenta tokens reales con la API")
    args = ap.parse_args()

    cv = load_cv()
    crudo = json.dumps(cv, ensure_ascii=False)

    entradas = []
    for seccion in tools._SECCIONES:
        for entrada in tools._entradas_de_seccion(cv, seccion):
            texto = tools._texto_de(entrada)
            entradas.append((entrada.get("id", "?"), len(texto), len(tools._tokenizar(texto))))

    print("=" * 68)
    print("  TAMANO DEL CORPUS")
    print("=" * 68)
    print(f"  JSON completo        {len(crudo):>8,} caracteres")
    print(f"  Entradas indexables  {len(entradas):>8,}")
    largos = sorted(entradas, key=lambda e: -e[1])
    print(f"  Entrada mas grande   {largos[0][1]:>8,} caracteres  ({largos[0][0]})")
    print(f"  Entrada mas chica    {largos[-1][1]:>8,} caracteres  ({largos[-1][0]})")
    print(f"  Promedio             {sum(e[1] for e in entradas) // len(entradas):>8,} caracteres")

    if args.tokens:
        import anthropic

        from app.config import MODEL, require_api_key

        cliente = anthropic.Anthropic(api_key=require_api_key())
        conteo = cliente.messages.count_tokens(
            model=MODEL, messages=[{"role": "user", "content": crudo}]
        )
        print(f"  Tokens reales        {conteo.input_tokens:>8,}  (medidos con la API, no estimados)")
        print(f"  De la ventana de 1M  {conteo.input_tokens / 10_000:>8.2f}%")

    print()
    print("=" * 68)
    print("  CALIDAD DE RECUPERACION")
    print("=" * 68)

    resultados = {}
    for nombre, banco in (("LEXICAS", LEXICAS), ("SEMANTICAS", SEMANTICAS)):
        print(f"\n  {nombre}  ({len(banco)} preguntas)")
        for k in (1, 3, 8):
            recall, fallos = _evaluar(banco, k)
            print(f"    recall@{k}   {recall:>6.0%}")
            if k == 8:
                resultados[nombre] = (recall, fallos)
        print(f"    MRR        {_rango_reciproco(banco):>6.2f}")

    print()
    print("=" * 68)
    print("  BRECHA LEXICA vs SEMANTICA")
    print("=" * 68)
    lex, sem = resultados["LEXICAS"][0], resultados["SEMANTICAS"][0]
    print(f"  recall@8 lexicas     {lex:>6.0%}")
    print(f"  recall@8 semanticas  {sem:>6.0%}")
    print(f"  brecha               {lex - sem:>6.0%}")
    print()
    print(f"  ADVERTENCIA sobre recall@8: con {len(entradas)} entradas, devolver 8 es")
    print("  entregar mas de un cuarto del CV. Un recall alto ahi mide poco: es")
    print("  facil acertar cuando devuelves esa fraccion del corpus. La senal")
    print("  real esta en el ranking.")
    print()

    mrr_lex = _rango_reciproco(LEXICAS)
    mrr_sem = _rango_reciproco(SEMANTICAS)
    r3_lex, _ = _evaluar(LEXICAS, 3)
    r3_sem, _ = _evaluar(SEMANTICAS, 3)
    print(f"  MRR lexicas          {mrr_lex:>6.2f}")
    print(f"  MRR semanticas       {mrr_sem:>6.2f}   (brecha {mrr_lex - mrr_sem:.2f})")
    print(f"  recall@3 semanticas  {r3_sem:>6.0%}   (lexicas {r3_lex:.0%})")
    print()

    if r3_sem >= 0.85 and mrr_sem >= 0.70:
        print("  VEREDICTO: lo lexico rankea bien incluso parafraseando.")
        print("  Un indice vectorial no resolveria un problema que hoy exista.")
    elif mrr_sem >= 0.55:
        print("  VEREDICTO: lo lexico NO falla, pero rankea peor en lenguaje natural.")
        print("  Los vectores mejorarian el ORDEN, no la cobertura. Como el modelo")
        print("  recibe 8 candidatos y escoge, ese error de orden no llega al usuario:")
        print("  la entrada correcta ya venia en el paquete. El costo de agregar")
        print("  embeddings no se paga con la mejora que produciria hoy.")
        print()
        print("  El dia que eso cambie es medible: si se baja k para ahorrar contexto,")
        print("  o si el corpus crece a un punto donde 8 candidatos ya no lo cubran.")
    else:
        print("  VEREDICTO: la busqueda lexica se queda corta en lenguaje natural.")
        print("  La recuperacion semantica se gano su lugar.")

    if resultados["SEMANTICAS"][1]:
        print("\n  Preguntas semanticas que fallaron:")
        for p in resultados["SEMANTICAS"][1]:
            print(f"    - {p}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
