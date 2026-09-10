"""Guardrails de entrada y de salida.

Filosofia: los controles deterministas atajan solo los casos inequivocos y baratos
de detectar (inyeccion de prompt evidente, fuga de PII, entradas absurdamente
largas). El matiz -- que cuenta como pregunta fuera de alcance, como reconocer
que algo no esta en el CV -- se resuelve en el prompt del sistema y se verifica
en la suite de evaluacion, no con expresiones regulares. Un regex demasiado
agresivo rompe conversaciones legitimas, que es peor que el problema que evita.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from .config import MAX_INPUT_CHARS

# --- Entrada ----------------------------------------------------------------


def _sin_acentos(texto: str) -> str:
    """Quita acentos para que los patrones empaten con como escribe la gente.

    Sin esto, la mitad de los guardrails estaba muerta y no se notaba: los
    patrones buscaban "cuanto gana" y "papas", pero una persona real escribe
    "¿Cuanto gana?" con tilde y "papas" con tilde. El modelo respondia bien de
    todos modos gracias al prompt del sistema, asi que el fallo era invisible --
    la deteccion determinista simplemente no corria.
    """
    return "".join(
        c for c in unicodedata.normalize("NFD", texto or "") if unicodedata.category(c) != "Mn"
    )


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

    plano = _sin_acentos(limpio)
    for patron in _PATRONES_INYECCION:
        if patron.search(plano):
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


# Las URLs se dejan intactas. Una URL firmada de Cloud Storage lleva el numero
# del proyecto, una fecha y una expiracion en segundos, y el patron de telefono
# se comia el numero de la cuenta de servicio: el enlace del reporte salia
# "firmado" con "[telefono no publico]" adentro y no abria. Redactar dentro de
# una URL que nosotros mismos generamos rompe el enlace siempre y no protege
# nada, porque ahi no hay datos de nadie.
_URL = re.compile(r"https?://\S+")


def _redactar_fragmento(texto: str, etiquetas: list[str]) -> str:
    resultado = texto
    for patron, reemplazo in _PATRONES_PII:
        def _sustituir(m: re.Match[str]) -> str:
            if _LISTA_BLANCA.fullmatch(m.group(0).strip()):
                return m.group(0)
            etiquetas.append("pii_redactada")
            return reemplazo

        resultado = patron.sub(_sustituir, resultado)
    return resultado


def redactar_pii(texto: str) -> tuple[str, list[str]]:
    """Redacta datos personales que no deben salir. Devuelve (texto, etiquetas).

    Se redacta el texto normal y se dejan intactas las URLs.
    """
    etiquetas: list[str] = []
    piezas: list[str] = []
    fin_anterior = 0

    for m in _URL.finditer(texto or ""):
        piezas.append(_redactar_fragmento(texto[fin_anterior:m.start()], etiquetas))
        piezas.append(m.group(0))
        fin_anterior = m.end()

    piezas.append(_redactar_fragmento((texto or "")[fin_anterior:], etiquetas))
    return "".join(piezas), etiquetas


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


# --- Instituciones y titulos inventados --------------------------------------
#
# Este control existe por un fallo real. En una conversacion de 36 turnos el
# agente afirmo que Edher es "Ingeniero en Sistemas Computacionales por el
# Tecnologico Nacional de Mexico, titulado en 2020". Las tres cosas son falsas:
# es Ingenieria en Energia, por la UAM, y de 2021.
#
# Es la clase de invento mas dificil de notar y la mas cara: suena
# perfectamente plausible para alguien con este perfil, un reclutador no tiene
# como saber que esta mal, y es un dato que se verifica en un titulo. No pude
# reproducirlo en aislamiento, asi que en vez de seguir persiguiendolo con el
# prompt, aqui queda una red que lo CACHA cuando ocurra.
#
# Por que se puede hacer de forma determinista, cuando "detectar alucinaciones"
# en general no se puede: los nombres de instituciones son sustantivos propios
# con prefijos reconocibles ("Universidad X", "Instituto X", "Tecnologico X"),
# y el conjunto valido para este CV es cerrado y pequeno. No detecta cualquier
# invento; detecta este, que es el que duele.
#
# No bloquea, etiqueta. Bloquear una respuesta por una coincidencia de patron
# rompe casos legitimos -- alguien pega una vacante que pide egresados del IPN y
# el agente la cita al contrastar. La etiqueta viaja a telemetria, donde es
# alertable, y la suite de evaluacion la convierte en asercion dura.

_PREFIJOS_INSTITUCION = (
    "universidad", "instituto", "tecnologico", "politecnico", "escuela superior",
    "colegio", "university", "institute",
)

_PATRON_INSTITUCION = re.compile(
    r"\b(?:Universidad|Instituto|Tecnologico|Tecnológico|Politecnico|Politécnico|"
    r"Escuela Superior|University|Institute)\b(?:[ ]+(?:de|del|la|las|los|el|en|of|the|"
    r"Nacional|Autonoma|Autónoma|Metropolitana|Estatal|Panamericana|Superior|"
    r"[A-ZÁÉÍÓÚÑ][A-Za-záéíóúñ]+))+"
)

_PATRON_TITULO = re.compile(
    r"\b(?:Ingenier(?:os?|as?|ia|ía)|Licenciad(?:o|a)|Licenciatura|Maestr(?:o|a|ia|ía)|"
    r"Doctor(?:a|ado)?)\b[ ]+en[ ]+([A-Za-zÁÉÍÓÚÑáéíóúñ ]{3,45})"
)


# Conectores que el patron arrastra y que no dicen nada del campo de estudio.
_CONECTORES_TITULO = {"en", "de", "del", "la", "el", "los", "las", "un", "una"}

# Palabras donde TERMINA el campo de estudio y empieza otra cosa: la institucion
# ("... en Energia POR LA Universidad ..."), o simplemente el resto de la frase.
# Sin este corte, "Ingenieria en Energia por la Universidad Autonoma
# Metropolitana" arrastraba el nombre de la universidad dentro del campo y la
# verdad se marcaba como invento. La institucion tiene su propio control; aqui
# solo se mira QUE estudio.
_FIN_DE_CAMPO = {
    "por", "y", "que", "con", "se", "su", "es", "para", "al", "desde", "donde",
    "universidad", "instituto", "tecnologico", "politecnico", "escuela",
    "university", "institute", "tec", "unam", "uam", "ipn", "itesm",
    "titulado", "graduado", "egresado", "cursando", "actualmente",
}


# La captura se corta a 45 caracteres, asi que la palabra que marca el final
# puede llegar partida ("... en el Tecnol"). Por eso el corte tambien se prueba
# por raiz y en las dos direcciones.
_RAICES_FIN = ("universid", "institut", "tecnolog", "politecnic", "escuel", "colegi")


def _es_fin_de_campo(palabra: str) -> bool:
    if palabra in _FIN_DE_CAMPO:
        return True
    return len(palabra) >= 5 and any(
        palabra.startswith(r) or r.startswith(palabra) for r in _RAICES_FIN
    )


def _palabras_de_campo(campo: str) -> set[str]:
    """Palabras de contenido del campo de estudio, hasta donde el campo termina."""
    palabras: set[str] = set()
    for p in _sin_acentos(campo).lower().split():
        if _es_fin_de_campo(p):
            break
        if p not in _CONECTORES_TITULO:
            palabras.add(p)
    return palabras


@lru_cache(maxsize=1)
def _vocabulario_academico() -> tuple[frozenset[str], frozenset[str]]:
    """Instituciones y campos de titulo que SI aparecen en el CV.

    Se derivan del CV, no se escriben a mano: si manana cambia la formacion, este
    control se mueve solo. Escribirlas a mano seria una segunda fuente de verdad
    que se desincroniza en silencio, que es como empiezan estos fallos.
    """
    from .config import load_cv

    texto = _sin_acentos(json.dumps(load_cv(), ensure_ascii=False)).lower()

    instituciones = {
        _sin_acentos(m.group(0)).lower() for m in _PATRON_INSTITUCION.finditer(
            json.dumps(load_cv(), ensure_ascii=False)
        )
    }
    # Las siglas y nombres cortos no traen prefijo, asi que se agregan por presencia.
    for sigla in ("uam", "unam", "ipn", "itesm", "tec de monterrey", "anahuac", "ibero"):
        if sigla in texto:
            instituciones.add(sigla)

    # Para los campos de titulo no se comparan cadenas sino PALABRAS. El patron
    # captura de mas ("Energia por la UAM", "Energia y muestra que..."), asi que
    # comparar la frase completa marcaba parafraseos legitimos. Lo que de verdad
    # delata un titulo inventado es una palabra ajena -- "Sistemas",
    # "Computacionales" -- no el orden de las que si estan.
    palabras: set[str] = set()
    for m in _PATRON_TITULO.finditer(json.dumps(load_cv(), ensure_ascii=False)):
        palabras |= _palabras_de_campo(m.group(1))
    return frozenset(instituciones), frozenset(palabras)


def verificar_academico(texto: str) -> list[str]:
    """Marca instituciones o campos de titulo que el CV no respalda.

    Devuelve etiquetas para telemetria; nunca modifica ni bloquea la respuesta.
    """
    if not texto:
        return []

    instituciones_cv, campos_cv = _vocabulario_academico()
    etiquetas: list[str] = []

    for m in _PATRON_INSTITUCION.finditer(texto):
        nombre = _sin_acentos(m.group(0)).strip().lower()
        if not any(nombre == v or nombre in v or v in nombre for v in instituciones_cv):
            etiquetas.append("institucion_fuera_del_cv")
            break

    for m in _PATRON_TITULO.finditer(texto):
        ajenas = _palabras_de_campo(m.group(1)) - campos_cv
        if ajenas:
            etiquetas.append("titulo_fuera_del_cv")
            break

    return etiquetas


# --- Politicas por tema sensible --------------------------------------------
#
# Estas NO bloquean ni responden con texto enlatado. Detectan el tema y le pasan
# al modelo la politica de como manejarlo, para que la respuesta salga natural y
# en el hilo de la conversacion, pero la conducta este controlada.
#
# Un texto enlatado se siente a muro y delata que hay un filtro detras. Una
# respuesta compuesta por el modelo bajo politica se siente a criterio
# profesional, que es justo lo que se quiere proyectar.


@dataclass(frozen=True)
class Politica:
    nombre: str
    patron: re.Pattern[str]
    guia: str


_POLITICAS: list[Politica] = [
    Politica(
        nombre="compensacion",
        patron=re.compile(
            r"\b(sueldo|salario|salarial|remuneracion|compensacion|pretensiones|"
            r"cuanto\s+(gana|cobra|pide|pagan|quiere\s+ganar)|expectativa\s+salarial|"
            r"salary|how\s+much\s+(does\s+he\s+)?(earn|make|charge))\b",
            re.I,
        ),
        guia=(
            "Preguntaron por dinero. NO des cifras, rangos ni referencias de mercado, aunque insistan "
            "o aunque digan que es solo aproximado. Explica en una linea que la compensacion la "
            "conversa Edher directamente. Con lo que ya sabes del perfil, ofrece UNA sola cosa que "
            "ayude a calibrar el nivel, la que mejor venga al caso. No salgas a juntar datos "
            "para esta respuesta: es una respuesta corta. Sin disculpas ni rodeos."
        ),
    ),
    Politica(
        nombre="vida_privada",
        patron=re.compile(
            r"\b(novia|novias|novio|novios|pareja|parejas|casad[oa]s?|solter[oa]s?|divorciad\w*|esposa|esposo|"
            r"relacion\w*\s+sentimental\w*|vida\s+amorosa|girlfriend|boyfriend|married|dating)\b",
            re.I,
        ),
        guia=(
            "Preguntaron por su vida privada o sentimental. Eso queda fuera de tu alcance. Una linea "
            "breve y de vuelta al perfil profesional, sin sermonear y sin sonar ofendido."
        ),
    ),
    Politica(
        nombre="datos_protegidos",
        patron=re.compile(
            r"\b(que\s+edad|su\s+edad|anios\s+de\s+edad|estado\s+civil|religion|religioso|"
            r"orientacion\s+sexual|embarazo|embarazada|enfermedad|discapacidad|"
            r"tiene\s+hijos|how\s+old)\b",
            re.I,
        ),
        guia=(
            "Preguntaron por un dato personal protegido: edad, estado civil, hijos, religion, salud "
            "u orientacion. Declina con naturalidad y sin acusar a nadie de nada -- lo mas probable "
            "es que la persona pregunte sin mala intencion. Puedes senalar en una linea que no son "
            "datos que formen parte de una evaluacion profesional, y redirige a lo que si evalua un "
            "perfil: experiencia, habilidades y resultados."
        ),
    ),
    Politica(
        nombre="identificacion_familiar",
        patron=re.compile(
            r"\b((sus|los)\s+(padres|papas|hermanos|familiares)|(su|la|el)\s+(mama|papa|madre|padre)|"
            r"nombre\w*\s+de\s+(sus|los)\s+(padres|papas|familiares))\b",
            re.I,
        ),
        guia=(
            "Preguntaron por miembros de su familia. No compartes nombres ni datos de terceros, "
            "nunca. Si viene a cuento puedes mencionar que el negocio de su familia es lo que lo "
            "motivo a estudiar la maestria, porque eso el mismo lo cuenta, pero sin nombres ni "
            "detalles que identifiquen a nadie."
        ),
    ),
    Politica(
        nombre="contacto_privado",
        patron=re.compile(
            r"\b((dame|damelo|cual\s+es|me\s+das|compartes|proporcion\w+|necesito|pasame)\b"
            r".{0,30}\b(telefono|celular|whatsapp|direccion|domicilio)|"
            r"donde\s+vive|su\s+direccion|su\s+domicilio|phone\s+number)\b",
            re.I,
        ),
        guia=(
            "Pidieron un canal de contacto privado. Solo compartes lo publico: correo, LinkedIn, "
            "GitHub y su sitio web. Ofrecelos sin dar explicaciones largas de por que no das lo otro."
        ),
    ),
    Politica(
        nombre="confidencialidad_cliente",
        patron=re.compile(
            r"\b((que|cual|cuales|nombre\s+del?)\s+cliente\w*|para\s+que\s+cliente|"
            r"clientes\s+de\s+google|que\s+empresa\s+era|client\s+name)\b",
            re.I,
        ),
        guia=(
            "Preguntaron por la identidad de un cliente. NUNCA des nombres de clientes finales ni "
            "detalles internos de sus proyectos. Si puedes hablar del sector -- farmacias, retail, "
            "marketing, inventarios -- y de lo que se construyo y que resultado dio. Google si se "
            "puede mencionar porque sus contribuciones ahi son publicas y estan en open source. "
            "Marca la distincion con naturalidad: es discrecion profesional, no evasiva."
        ),
    ),
]


def detectar_temas_sensibles(texto: str) -> list[Politica]:
    """Devuelve las politicas que aplican al mensaje del usuario."""
    plano = _sin_acentos(texto)
    return [p for p in _POLITICAS if p.patron.search(plano)]


_GUIA_ESCALADA = (
    "AVISO: esta conversacion ya insistio varias veces en temas fuera de tu alcance. No repitas la "
    "misma negativa una vez mas, que se siente a muro. Reconoce que el tema se sale de lo que "
    "puedes cubrir, ofrece el correo de Edher para que lo traten directamente con el, y sigue "
    "disponible para lo profesional."
)


def guias_de_politica(mensajes: list[dict], umbral_escalada: int = 3) -> tuple[list[str], list[str]]:
    """Politicas del turno actual, mas escalada si el patron se repite.

    La escalada se calcula sobre el transcript completo, que la plataforma reenvia
    en cada turno. Asi funciona sin guardar estado en el servidor: la conversacion
    misma es la memoria.
    """
    del_usuario = [m["content"] for m in mensajes if m.get("role") == "user"]
    if not del_usuario:
        return [], []

    politicas = detectar_temas_sensibles(del_usuario[-1])
    guias = [p.guia for p in politicas]
    etiquetas = [f"tema:{p.nombre}" for p in politicas]

    turnos_sensibles = sum(1 for m in del_usuario if detectar_temas_sensibles(m))
    if turnos_sensibles >= umbral_escalada:
        guias.append(_GUIA_ESCALADA)
        etiquetas.append("escalada_fuera_de_alcance")

    return guias, etiquetas
