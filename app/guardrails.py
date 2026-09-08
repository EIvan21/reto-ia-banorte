"""Guardrails de entrada y de salida.

Filosofia: los controles deterministas atajan solo los casos inequivocos y baratos
de detectar (inyeccion de prompt evidente, fuga de PII, entradas absurdamente
largas). El matiz -- que cuenta como pregunta fuera de alcance, como reconocer
que algo no esta en el CV -- se resuelve en el prompt del sistema y se verifica
en la suite de evaluacion, no con expresiones regulares. Un regex demasiado
agresivo rompe conversaciones legitimas, que es peor que el problema que evita.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .config import MAX_INPUT_CHARS

# --- Entrada ----------------------------------------------------------------

# Intentos claros de secuestrar las instrucciones del sistema. Se exige una
# senal fuerte (verbo de anulacion + objeto que se refiere a las instrucciones)
# para no marcar preguntas legitimas que solo mencionen la palabra "instrucciones".
_PATRONES_INYECCION: list[re.Pattern[str]] = [
    re.compile(r"\b(ignora|olvida|descarta)\s+(todas?\s+)?(las?\s+)?(instruccion|indicacion|regla|orden)", re.I),
    re.compile(r"\b(ignore|forget|disregard|override)\s+(all\s+)?(your\s+|the\s+|previous\s+)*(instruction|prompt|rule|direction)", re.I),
    re.compile(r"\b(revela|muestra|imprime|repite|dame)\s+(tu|el|las)\s+(system\s*prompt|prompt\s+del\s+sistema|instrucciones\s+del\s+sistema)", re.I),
    re.compile(r"\b(reveal|show|print|repeat|output)\s+(your|the)\s+(system\s*prompt|instructions|initial\s+prompt)", re.I),
    re.compile(r"\bahora\s+eres\s+(un|una)\b.{0,40}\b(no\s+eres|deja\s+de\s+ser)\b", re.I),
    re.compile(r"\b(you\s+are\s+now|from\s+now\s+on\s+you\s+are)\b.{0,40}\b(not|no\s+longer)\b", re.I),
    re.compile(r"\bmodo\s+(desarrollador|dios|sin\s+restricciones)\b", re.I),
    re.compile(r"\b(developer|god|jailbreak|DAN)\s+mode\b", re.I),
]

_RESPUESTA_INYECCION = (
    "Soy el agente conversacional del CV de Edher Diaz y opero con un alcance fijo: "
    "hablar de su perfil, experiencia, habilidades y proyectos. No puedo cambiar de rol "
    "ni exponer mi configuracion interna.\n\n"
    "Con gusto te ayudo con lo que si me toca. Por ejemplo: su experiencia con modelos de "
    "lenguaje, sus proyectos open source en Google, o que tan bien encaja con una vacante "
    "que quieras pegarme."
)


@dataclass
class Veredicto:
    """Resultado de revisar una entrada."""

    permitido: bool
    motivo: str = ""
    respuesta_segura: str = ""
    etiquetas: list[str] = field(default_factory=list)


def revisar_entrada(texto: str) -> Veredicto:
    """Revisa el mensaje del usuario antes de gastar una llamada al modelo."""
    limpio = (texto or "").strip()

    if not limpio:
        return Veredicto(
            permitido=False,
            motivo="entrada_vacia",
            respuesta_segura="No recibi ninguna pregunta. ¿Que te gustaria saber del perfil de Edher?",
            etiquetas=["entrada_vacia"],
        )

    if len(limpio) > MAX_INPUT_CHARS:
        return Veredicto(
            permitido=False,
            motivo="entrada_demasiado_larga",
            respuesta_segura=(
                f"Tu mensaje supera el limite de {MAX_INPUT_CHARS} caracteres. "
                "¿Puedes resumirlo o mandarlo por partes?"
            ),
            etiquetas=["entrada_demasiado_larga"],
        )

    for patron in _PATRONES_INYECCION:
        if patron.search(limpio):
            return Veredicto(
                permitido=False,
                motivo="inyeccion_de_prompt",
                respuesta_segura=_RESPUESTA_INYECCION,
                etiquetas=["inyeccion_de_prompt"],
            )

    return Veredicto(permitido=True)


# --- Salida -----------------------------------------------------------------

# Telefonos mexicanos e internacionales en sus formatos usuales. El CV real trae
# un telefono; el JSON del agente lo omite a proposito, y esto es la segunda
# barrera por si llegara a colarse desde el historial de conversacion.
_PATRONES_PII: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\+?\d{1,3}[\s.-]?\(?\d{2,3}\)?[\s.-]?\d{3,4}[\s.-]?\d{4}\b"), "[telefono no publico]"),
    (re.compile(r"\b\d{16}\b"), "[dato redactado]"),
]

# Anios y cifras legitimas del CV que NO deben confundirse con telefonos.
_LISTA_BLANCA = re.compile(r"\b(19|20)\d{2}\b")


def redactar_pii(texto: str) -> tuple[str, list[str]]:
    """Redacta datos personales que no deben salir. Devuelve (texto, etiquetas)."""
    etiquetas: list[str] = []
    resultado = texto

    for patron, reemplazo in _PATRONES_PII:
        def _sustituir(m: re.Match[str]) -> str:
            if _LISTA_BLANCA.fullmatch(m.group(0).strip()):
                return m.group(0)
            etiquetas.append("pii_redactada")
            return reemplazo

        resultado = patron.sub(_sustituir, resultado)

    return resultado, etiquetas


# Senales de que la respuesta afirma hechos concretos del CV.
_SENALES_FACTUALES = re.compile(
    r"\b(GlobalLogic|GTEC|Infosys|Looker|LookML|BigQuery|Tecnologico de Monterrey|"
    r"Associate Cloud Engineer|30%|40%|4\.5/5|400\+?)\b",
    re.I,
)


def verificar_fundamento(texto: str, citas: list[str]) -> list[str]:
    """Detecta respuestas que afirman hechos del CV sin haber consultado ninguna herramienta.

    No bloquea: emite una etiqueta que viaja a la telemetria. Bloquear aqui daria
    falsos positivos en saludos y preguntas meta. La suite de evaluacion es la que
    convierte esto en una asercion dura sobre las preguntas que si son del CV.
    """
    if citas:
        return []
    if _SENALES_FACTUALES.search(texto or ""):
        return ["afirmacion_sin_fundamento"]
    return []
