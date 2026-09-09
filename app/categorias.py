"""Clasifica de que trata cada turno, sin guardar el texto.

Esto responde la pregunta que un dueno de agente realmente quiere contestar:
"¿que le preguntan a mi agente?". Es la misma metrica que mide el Agent Analytics
Block sobre los agentes de los clientes de Google, aplicada al propio agente.

Decision de privacidad: se guarda la CATEGORIA, nunca el texto. Quien escribe es
un tercero -- un reclutador, un manager -- que no dio permiso para almacenar lo
que escribio. La categoria da la senal analitica completa sin ese costo.

Decision de costo: la clasificacion NO usa el modelo. Sale de senales que el
turno ya produjo:

  1. Si un guardrail de tema disparo, esa es la categoria. Es la senal mas fuerte
     porque viene de una deteccion explicita.
  2. Si no, las herramientas que el modelo eligio llamar. Si consulto
     obtener_experiencia, la pregunta era de experiencia. Gratis y preciso: el
     modelo ya hizo el trabajo de entender la intencion.
  3. Y solo si no hubo ninguna de las dos, unas cuantas palabras clave para
     distinguir saludo, meta y fuera de alcance.

Una llamada extra al modelo para clasificar costaria tokens y latencia en cada
turno, por una senal que ya estaba ahi.
"""

from __future__ import annotations

import re
import unicodedata

# Herramienta -> categoria. El modelo ya decidio la intencion al elegirla.
_POR_HERRAMIENTA: dict[str, str] = {
    "obtener_experiencia": "experiencia",
    "obtener_habilidades": "habilidades",
    "obtener_proyectos": "proyectos",
    "evaluar_vacante": "vacante",
    "obtener_contacto": "contacto",
    "generar_reporte": "reporte",
    "servidor:web_fetch": "vacante",
}

# Politica de tema -> categoria. Es la senal mas fuerte que hay.
_POR_POLITICA: dict[str, str] = {
    "tema:compensacion": "compensacion",
    "tema:vida_privada": "personal_fuera_de_alcance",
    "tema:datos_protegidos": "datos_protegidos",
    "tema:identificacion_familiar": "personal_fuera_de_alcance",
    "tema:contacto_privado": "contacto_privado",
    "tema:confidencialidad_cliente": "confidencialidad",
    "inyeccion_de_prompt": "inyeccion",
}

_SALUDO = re.compile(r"^\s*(hola|buenas|buenos dias|buenas tardes|hey|hi|hello|que tal)\b", re.I)
_META = re.compile(r"\b(quien eres|que eres|como funcionas|eres un bot|eres humano|"
                   r"who are you|what are you|are you (a )?(bot|human))\b", re.I)
_INTERESES = re.compile(r"\b(hobby|hobbies|aficion|tiempo libre|magia|guitarra|correr|"
                        r"gimnasio|tiktok|musica|deporte)\b", re.I)
_FORMACION = re.compile(r"\b(estudi|universidad|maestria|licenciatura|carrera|"
                        r"certificacion|diplomado|curso)\w*\b", re.I)


def _plano(texto: str) -> str:
    texto = unicodedata.normalize("NFD", (texto or "").lower())
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


def clasificar(
    pregunta: str,
    herramientas: list[str] | None = None,
    etiquetas: list[str] | None = None,
) -> str:
    """Devuelve una sola categoria para el turno."""
    # 1. Un guardrail que disparo es la senal mas confiable.
    for etiqueta in etiquetas or []:
        if etiqueta in _POR_POLITICA:
            return _POR_POLITICA[etiqueta]

    # 2. Las herramientas que el modelo eligio. Se toma la mas especifica: si
    #    contrasto una vacante, eso describe el turno mejor que "experiencia",
    #    aunque de paso haya consultado la experiencia.
    usadas = [h for h in (herramientas or []) if h in _POR_HERRAMIENTA]
    for preferida in ("evaluar_vacante", "generar_reporte", "obtener_contacto"):
        if preferida in usadas:
            return _POR_HERRAMIENTA[preferida]
    if usadas:
        return _POR_HERRAMIENTA[usadas[0]]

    # 3. Sin herramientas ni guardrail: unas pocas palabras clave.
    plano = _plano(pregunta)
    if _SALUDO.match(plano):
        return "saludo"
    if _META.search(plano):
        return "meta_agente"
    if _INTERESES.search(plano):
        return "intereses"
    if _FORMACION.search(plano):
        return "formacion"
    if "buscar_cv" in (herramientas or []):
        return "perfil_general"
    return "fuera_de_alcance"


# Todas las categorias posibles. La prueba verifica que la clasificacion nunca
# devuelva algo que no este aqui: una categoria nueva sin declarar aparece como
# un valor huerfano en el dashboard y nadie sabe de donde salio.
CATEGORIAS = frozenset({
    "experiencia", "habilidades", "proyectos", "vacante", "contacto", "reporte",
    "compensacion", "personal_fuera_de_alcance", "datos_protegidos",
    "contacto_privado", "confidencialidad", "inyeccion", "saludo", "meta_agente",
    "intereses", "formacion", "perfil_general", "fuera_de_alcance",
})
