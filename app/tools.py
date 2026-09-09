"""Herramientas que el modelo usa para consultar el CV.

Decision de diseno: NO hay base vectorial, y la decision esta medida, no opinada.
`evals/medir_corpus.py` compara la recuperacion contra dos bancos de preguntas:
unas que comparten vocabulario con el CV y otras parafraseadas, como habla quien
no lo ha leido. Los numeros con 27 entradas y 12,940 tokens reales:

    recall@8   100% lexicas | 100% semanticas
    recall@3    92% lexicas |  75% semanticas
    MRR        0.74 lexicas | 0.61 semanticas

Lo lexico NO falla: rankea peor en lenguaje natural. Los vectores mejorarian el
ORDEN, no la cobertura, y como el modelo recibe 8 candidatos y escoge, ese error
de orden no llega al usuario -- la entrada correcta ya venia en el paquete.

Ademas, el enfoque por herramientas compra tres cosas que un indice de embeddings
no da:

  1. Trazabilidad: cada resultado trae el 'id' de la entrada que lo respaldo,
     asi que se puede verificar de donde salio cada afirmacion.
  2. Determinismo: la misma pregunta devuelve exactamente el mismo contexto,
     lo cual hace que la suite de evaluacion sea reproducible.
  3. Cero infraestructura extra que operar, respaldar y pagar.

Cuando esto deje de ser cierto es medible, no intuitivo: si se baja k para
ahorrar contexto, o si el corpus crece hasta que 8 candidatos ya no lo cubran.
Correr medir_corpus.py responde la pregunta en segundos.

Todas las herramientas devuelven un dict con la llave "_citas": la lista de ids
del CV que fundamentan el resultado. El guardrail de salida verifica que la
respuesta final se apoye en al menos una cita.
"""

from __future__ import annotations

import math
import re
import unicodedata
from functools import lru_cache
from typing import Any, Callable

from .config import load_cv

# --- Utilidades de busqueda -------------------------------------------------

# Grupos de equivalencia ES/EN. Son GRUPOS, no un diccionario direccional: si la
# consulta toca cualquier miembro, se expande a todo el grupo. Una tabla
# direccional fallaba en el caso mas importante -- alguien pregunta "modelos de
# lenguaje" y la clave era "llm", asi que la frase real de una persona nunca
# llegaba a la experiencia con Gemini y Claude.
# Los miembros de varias palabras se detectan como frase dentro de la consulta,
# porque no sobreviven a la tokenizacion.
_GRUPOS_SINONIMOS: list[set[str]] = [
    {"llm", "llms", "modelo de lenguaje", "modelos de lenguaje", "language model",
     "language models", "lenguaje", "language", "gemini", "claude", "codex", "cursor", "gpt"},
    {"ia", "ai", "inteligencia artificial", "artificial intelligence", "genai",
     "generativa", "generative"},
    {"agente", "agentes", "agent", "agents", "agentico", "agentica", "agentic"},
    {"nube", "cloud", "gcp", "google cloud"},
    {"despliegue", "desplegar", "desplegando", "deploy", "deployment", "produccion",
     "cloud run", "docker", "contenedor", "contenedores"},
    {"pronostico", "forecast", "forecasting", "prediccion", "predictivo", "predictiva"},
    {"tablero", "tableros", "dashboard", "dashboards"},
    {"experiencia", "trabajo", "trabajando", "trabajado", "empleo", "laboral",
     "trayectoria", "carrera", "experience"},
    {"proyecto", "proyectos", "project", "projects", "portafolio", "portfolio"},
    {"certificacion", "certificaciones", "certificado", "certification", "certifications"},
    {"estudios", "educacion", "escuela", "universidad", "maestria", "licenciatura",
     "titulo", "education", "degree"},
    {"habilidad", "habilidades", "tecnologia", "tecnologias", "skill", "skills",
     "stack", "dominas", "domina", "dominio"},
    {"rol", "roles", "puesto", "vacante", "posicion", "position", "role", "buscando"},
    {"open source", "opensource", "contribucion", "contribuciones", "contributions"},
    {"datos", "data", "dato"},
    {"sql", "bigquery", "consulta", "consultas", "query", "queries"},
    {"analitica", "analytics", "analisis"},
    {"hobby", "hobbies", "aficion", "aficiones", "intereses", "interes",
     "tiempo libre", "pasatiempo", "pasatiempos", "fuera del trabajo", "personal",
     "magia", "guitarra", "correr", "gimnasio", "musica", "deporte"},
    {"contenido", "tiktok", "video", "videos", "generacion de video", "veo", "sora"},
    {"historia", "trayectoria", "camino", "recorrido", "empezo", "empezaste", "comenzo",
     "llego", "llegaste", "origen", "background", "story"},
    {"porque", "razon", "razones", "motivo", "motivos", "decidio", "decidiste",
     "cambio", "cambiar", "salio", "saliste", "dejo", "dejaste", "why"},
    {"orgulloso", "orgullo", "logro", "logros", "satisfaccion", "mejor", "destacado"},
    {"trabajas", "trabaja", "metodo", "forma de trabajar", "estilo", "colabora",
     "colaboracion", "equipo", "equipos", "comunidad", "cliente", "clientes"},
    {"energia", "desaladora", "osmosis", "turbina", "planta", "arduino", "raspberry",
     "sensores", "hardware"},
    {"soporte", "tickets", "casos", "sme", "atencion", "clientes molestos"},
    {"debilidad", "debilidades", "defecto", "defectos", "cuesta", "dificil",
     "limitacion", "limitaciones", "flaqueza", "weakness", "mejorar"},
    {"fortaleza", "fortalezas", "fuerte", "bueno", "reconocen", "strength", "destaca"},
    {"liderazgo", "lider", "liderar", "lidera", "mentoria", "mentor", "leadership"},
    {"jefe", "manager", "lider", "supervisor", "ambiente", "cultura", "boss"},
    {"ayuda", "pedir ayuda", "preguntar", "duda", "dudas", "aprender", "aprende",
     "investigar", "investigacion", "estudiar"},
    {"multiagente", "multi agente", "orquestacion", "flujo", "flujos", "workflow",
     "pipeline de agentes", "equipo de agentes", "roles"},
    {"guion", "guionista", "director", "camara", "camaras", "toma", "tomas", "escena",
     "escenas", "editor", "edicion", "clip", "clips"},
    {"lento", "lentitud", "rendimiento", "performance", "optimizar", "optimizacion",
     "auditar", "auditoria", "cuello de botella"},
    {"repetitivo", "automatizar", "automatizacion", "andamiaje", "scaffolding",
     "plantilla", "estructura"},
    {"motivacion", "motiva", "motivo", "meta", "metas", "futuro", "anos", "anios",
     "planes", "aspiracion", "aspiraciones", "vision", "goals"},
    {"pyme", "pymes", "negocio", "negocios", "emprendimiento", "familia", "familiar",
     "pequenas empresas", "pequena empresa"},
    {"divulgacion", "ensenar", "explicar", "compartir", "contenido", "creador"},
    {"publicacion", "publicaciones", "articulo", "paper", "investigacion", "cfd",
     "simulacion", "compresor", "publicado"},
    {"idioma", "idiomas", "ingles", "japones", "espanol", "language", "languages"},
    {"curso", "cursos", "diplomado", "platzi", "autodidacta", "formacion"},
]


# Palabras vacias del espanol y el ingles. Sin esta lista, terminos como "con"
# empatan dentro de "Construyo" o "Consultant" y el ruido entierra a la senal:
# la pregunta "experiencia con modelos de lenguaje" llegaba a devolver el puesto
# equivocado porque "con" aparecia mas veces ahi.
_VACIAS = {
    "con", "para", "que", "los", "las", "del", "una", "uno", "por", "sus", "mas",
    "como", "cual", "cuales", "tiene", "tienes", "sobre", "entre", "este", "esta",
    "estas", "estos", "hay", "son", "fue", "han", "the", "and", "for", "with",
    "what", "your", "you", "has", "have", "any", "how", "does", "did", "about",
    "cuenta", "cuentame", "dime", "explica", "hablame", "algun", "alguna",
}


def _normalizar(texto: str) -> str:
    """Minusculas y sin acentos, para que 'pronóstico' y 'pronostico' empaten."""
    texto = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in texto if unicodedata.category(c) != "Mn")


_SEPARADOR = re.compile(r"[^a-z0-9+#.]+")


def _tokenizar(texto: str) -> list[str]:
    """Parte texto normalizado en palabras. Conserva '+', '#' y '.' porque
    distinguen tecnologias reales: 'c++', 'c#', 'node.js', '4.5'."""
    return [t for t in _SEPARADOR.split(_normalizar(texto)) if t]


def _terminos_de_consulta(consulta: str) -> set[str]:
    """Tokens utiles de la consulta: sin palabras vacias y sin fragmentos cortos."""
    return {t for t in _tokenizar(consulta) if len(t) > 2 and t not in _VACIAS}


# Llaves que solo dan estructura y no significado. Todas las demas SI se indexan.
_LLAVES_ESTRUCTURALES = {
    "id", "actual", "inicio", "fin", "publico", "en_curso", "anio", "nivel",
    "tipo", "periodo", "periodo_texto", "version", "items",
}


def _texto_de(entrada: Any) -> str:
    """Aplana cualquier entrada del CV a texto plano buscable.

    Se indexan tambien los NOMBRES de los campos, no solo sus valores. En este CV
    los nombres son practicamente la pregunta que cada campo responde
    ('de_que_esta_orgulloso', 'por_que_sali', 'como_llegue', 'dia_a_dia'), asi que
    ignorarlos costaba coincidencias obvias: preguntar "¿de que se siente
    orgulloso?" no recuperaba la entrada que literalmente tiene ese campo, porque
    la palabra vivia en la llave y no en el texto.
    """
    if isinstance(entrada, dict):
        partes: list[str] = []
        for k, v in entrada.items():
            if k.startswith("_"):
                continue
            if k not in _LLAVES_ESTRUCTURALES:
                partes.append(k.replace("_", " "))
            partes.append(_texto_de(v))
        return " ".join(partes)
    if isinstance(entrada, list):
        return " ".join(_texto_de(v) for v in entrada)
    if isinstance(entrada, (str, int, float)):
        return str(entrada)
    return ""


# Pesos por tipo de coincidencia, ordenados por confianza. La separacion importa:
# con un limite fijo de resultados, una coincidencia debil no solo suma poco, sino
# que puede DESPLAZAR a una fuerte fuera del corte. Aprendido a la mala -- al
# empatar raices, "open source" empujo al proyecto correcto al septimo lugar.
_PESO_EXACTO = 4   # la palabra aparece tal cual
_PESO_PREFIJO = 2  # una extiende a la otra: modelo/modelos
_PESO_RAIZ = 1     # misma raiz, final distinto: revision/revisor
_PESO_FRASE = 3    # frase de varias palabras presente literal
_MAX_RESULTADOS = 8


def _es_variante(termino: str, palabra: str) -> bool:
    """Empata variantes morfologicas de la misma raiz.

    Dos casos distintos:

      1. Una palabra extiende a la otra: modelo/modelos, agente/agentes.
      2. Ambas salen de la misma raiz pero divergen al final: revision/revisor,
         generacion/generador, optimizar/optimizacion. El caso 1 no las atrapa
         porque ninguna empieza con la otra, y por eso "revision automatica" no
         recuperaba la entrada llena de "revisor" y "revisa".

    El umbral de raiz compartida es 5 caracteres con ambas palabras de 6 o mas.
    Deja pasar algun falso positivo ocasional, y esta bien: un falso positivo solo
    agrega una entrada floja que el modelo descarta, mientras que un falso
    negativo pierde la respuesta correcta por completo. Los costos no son
    simetricos, asi que se prefiere recuperar de mas.
    """
    if len(termino) < 4 or len(palabra) < 4:
        return False
    if palabra.startswith(termino) or termino.startswith(palabra):
        return True
    if len(termino) >= 6 and len(palabra) >= 6:
        comunes = 0
        for a, b in zip(termino, palabra):
            if a != b:
                break
            comunes += 1
        return comunes >= 5
    return False


def _expandir_consulta(consulta: str) -> set[str]:
    """Tokens de la consulta mas todos los grupos de sinonimos que toca.

    Un grupo se activa si la consulta contiene alguno de sus miembros: como token
    exacto, como variante morfologica, o como frase literal en el caso de los
    miembros de varias palabras.
    """
    normalizada = _normalizar(consulta)
    terminos = _terminos_de_consulta(consulta)
    expandidos = set(terminos)

    for grupo in _GRUPOS_SINONIMOS:
        activo = False
        for miembro in grupo:
            if " " in miembro:
                if miembro in normalizada:
                    activo = True
                    break
            elif miembro in terminos or any(_es_variante(miembro, t) for t in terminos):
                activo = True
                break
        if activo:
            expandidos |= grupo

    return expandidos


# Tope para terminos que no aparecen en el CV: no se les puede calcular IDF, y
# conviene que pesen como algo raro y no como algo comun.
_IDF_MAXIMO = 3.0


@lru_cache(maxsize=1)
def _idf() -> dict[str, float]:
    """IDF por token sobre las entradas del CV.

    Es el arreglo al sintoma medido: conforme el CV crecio en texto narrativo,
    palabras como "open source", "agentes" o "datos" empezaron a aparecer en casi
    todas las entradas, los puntajes se aplanaron (siete entradas empatadas en 13)
    y la busqueda dejo de distinguir entre una entrada que ES un proyecto open
    source y una que solo lo menciona.

    IDF penaliza lo que esta en todas partes y premia lo que discrimina. Con 26
    entradas se calcula una vez por proceso y es instantaneo.
    """
    from collections import Counter

    cv = load_cv()
    documentos: list[set[str]] = []
    for seccion in _SECCIONES:
        for entrada in _entradas_de_seccion(cv, seccion):
            documentos.append(set(_tokenizar(_texto_de(entrada))))

    n = len(documentos) or 1
    frecuencia: Counter[str] = Counter()
    for doc in documentos:
        frecuencia.update(doc)

    return {
        token: min(_IDF_MAXIMO, math.log((n + 1) / (df + 1)) + 1.0)
        for token, df in frecuencia.items()
    }


def _puntuar(entrada: Any, terminos: set[str]) -> float:
    """Puntua una entrada del CV contra los terminos de la consulta.

    La coincidencia es por palabra completa, no por subcadena: de lo contrario
    "con" empata dentro de "Construyo" y el ruido gana. La exacta pesa el doble
    que la variante morfologica, y las frases de varias palabras (por ejemplo
    "google cloud") se buscan como subcadena porque no sobreviven a la
    tokenizacion.
    """
    texto = _normalizar(_texto_de(entrada))
    palabras = set(_tokenizar(texto))
    idf = _idf()

    puntaje = 0.0
    for t in terminos:
        if not t:
            continue
        if " " in t:
            if t in texto:
                # Las frases discriminan por si solas; se les da el IDF de su
                # token mas raro.
                peso_idf = min((idf.get(x, _IDF_MAXIMO) for x in t.split()), default=1.0)
                puntaje += _PESO_FRASE * peso_idf
        elif t in palabras:
            puntaje += _PESO_EXACTO * idf.get(t, _IDF_MAXIMO)
        elif any(p.startswith(t) or t.startswith(p) for p in palabras if len(p) >= 4):
            puntaje += _PESO_PREFIJO * idf.get(t, _IDF_MAXIMO)
        elif any(_es_variante(t, p) for p in palabras):
            puntaje += _PESO_RAIZ * idf.get(t, _IDF_MAXIMO)

    # Normalizacion por longitud. Sin esto las entradas narrativas largas
    # (trayectoria, forma_de_trabajar) ganan por acumulacion: mas texto es mas
    # superficie donde caer, aunque el tema central sea otro.
    return puntaje / (1.0 + math.log(1 + len(palabras) / 50))


@lru_cache(maxsize=1)
def _vocabulario_cv() -> frozenset[str]:
    """Todas las palabras que aparecen en el CV. Se calcula una vez por proceso."""
    return frozenset(_tokenizar(_texto_de(load_cv())))


# Tecnologias que la gente suele escribir en minusculas. Sirven de respaldo a la
# deteccion por mayuscula.
_TECNOLOGIAS_CONOCIDAS = {
    "kubernetes", "k8s", "docker", "terraform", "ansible", "jenkins", "airflow",
    "dbt", "snowflake", "databricks", "redshift", "synapse", "kafka", "spark",
    "hadoop", "flink", "mongodb", "cassandra", "redis", "elasticsearch", "neo4j",
    "postgres", "postgresql", "mysql", "oracle", "sqlserver", "tableau", "powerbi",
    "qlik", "superset", "metabase", "sas", "spss", "matlab", "scala", "rust", "golang",
    "java", "kotlin", "swift", "ruby", "php", "perl", "react", "angular", "vue",
    "django", "flask", "spring", "dotnet", "aws", "azure", "lambda", "sagemaker",
    "pytorch", "tensorflow", "keras", "langchain", "llamaindex", "huggingface",
    "pinecone", "weaviate", "chroma", "qdrant", "milvus", "openai", "anthropic",
    "mlflow", "kubeflow", "sap", "salesforce", "informatica", "talend", "cobol",
}

_SIGNOS_TECNICOS = re.compile(r"[0-9+#.]")


def _parece_tecnologia(termino: str, consulta_cruda: str) -> bool:
    """Decide si un termino es un nombre de tecnologia y no vocabulario comun.

    Tres senales, en orden de confianza: aparece con mayuscula a media frase
    (asi se escriben los nombres propios), trae digitos o simbolos tecnicos, o
    esta en la lista de tecnologias que suelen escribirse en minusculas.
    """
    if termino in _TECNOLOGIAS_CONOCIDAS:
        return True
    if _SIGNOS_TECNICOS.search(termino):
        return True

    # Mayuscula a media frase: se descarta la primera palabra de cada oracion.
    for oracion in re.split(r"[.!?¿¡\n]", consulta_cruda):
        palabras = oracion.split()
        for i, palabra in enumerate(palabras):
            limpia = palabra.strip("¿?¡!,;:()\"'")
            if i > 0 and limpia[:1].isupper() and _normalizar(limpia) == termino:
                return True
    return False


def _terminos_ausentes(consulta: str) -> list[str]:
    """Nombres de tecnologia de la consulta que no aparecen en ninguna parte del CV.

    Es la senal explicita de "esto no lo tengo": si alguien pregunta por Snowflake
    o Kubernetes, el modelo recibe el dato duro de que esa palabra no existe en el
    CV, en vez de tener que deducirlo de la ausencia de resultados relevantes.

    Solo se reportan terminos que parecen tecnologias. Marcar vocabulario comun
    ("carrera", "desafiante") llevaria al modelo a negar experiencia en cosas que
    ni siquiera son tecnologias, que es peor que no avisar nada: ante la duda,
    callar es el modo seguro.
    """
    vocabulario = _vocabulario_cv()
    ausentes = []
    for t in _terminos_de_consulta(consulta):
        if t in vocabulario or any(_es_variante(t, p) for p in vocabulario):
            continue
        if _parece_tecnologia(t, consulta):
            ausentes.append(t)
    return sorted(ausentes)


# Secciones del CV que la busqueda recorre, con su etiqueta legible.
_SECCIONES: dict[str, str] = {
    "perfil": "perfil",
    "experiencia": "experiencia",
    "proyectos": "proyectos",
    "habilidades": "habilidades",
    "educacion": "educacion",
    "certificaciones": "certificaciones",
    "open_source": "open_source",
    "preferencias_rol": "preferencias_rol",
    "intereses": "intereses",
    "trayectoria": "trayectoria",
    "forma_de_trabajar": "forma_de_trabajar",
    "publicaciones": "publicaciones",
    "formacion_complementaria": "formacion_complementaria",
}


def _entradas_de_seccion(cv: dict, seccion: str) -> list[dict]:
    valor = cv.get(seccion)
    if valor is None:
        return []
    return valor if isinstance(valor, list) else [valor]


# --- Implementacion de las herramientas -------------------------------------


def buscar_cv(consulta: str, seccion: str = "") -> dict:
    """Busqueda por palabra clave sobre el CV completo."""
    cv = load_cv()
    terminos = _expandir_consulta(consulta)

    secciones = [seccion] if seccion and seccion in _SECCIONES else list(_SECCIONES)

    resultados: list[dict] = []
    for sec in secciones:
        for entrada in _entradas_de_seccion(cv, sec):
            puntaje = _puntuar(entrada, terminos)
            if puntaje > 0:
                resultados.append({"seccion": sec, "puntaje": puntaje, "contenido": entrada})

    resultados.sort(key=lambda r: r["puntaje"], reverse=True)
    # El limite crece con el corpus. Con 6 entradas empezaron a quedarse fuera
    # coincidencias validas en cuanto el CV paso de 3k a 7k tokens.
    resultados = resultados[:_MAX_RESULTADOS]

    ausentes = _terminos_ausentes(consulta)

    if not resultados:
        return {
            "encontrado": False,
            "terminos_ausentes_del_cv": ausentes,
            "mensaje": (
                "No hay informacion en el CV que responda a esa consulta. "
                "Dilo explicitamente en lugar de inferir o inventar."
            ),
            "_citas": [],
        }

    salida = {
        "encontrado": True,
        "resultados": [r["contenido"] for r in resultados],
        "_citas": [r["contenido"].get("id") for r in resultados if r["contenido"].get("id")],
    }
    if ausentes:
        # Aviso explicito: estas palabras de la pregunta no existen en el CV.
        # Los resultados de arriba salieron por otros terminos, no por estas.
        salida["terminos_ausentes_del_cv"] = ausentes
        salida["aviso"] = (
            "Estos terminos de la pregunta NO aparecen en ninguna parte del CV: "
            + ", ".join(ausentes)
            + ". Di explicitamente que no hay experiencia documentada en eso. "
            "Los resultados de abajo empataron por otras palabras de la pregunta."
        )
    return salida


def obtener_experiencia(empresa: str = "") -> dict:
    """Trayectoria laboral, opcionalmente filtrada por empresa."""
    cv = load_cv()
    puestos = cv["experiencia"]

    if empresa:
        aguja = _normalizar(empresa)
        filtrados = [p for p in puestos if aguja in _normalizar(p["empresa"])]
        if not filtrados:
            disponibles = ", ".join(p["empresa"] for p in puestos)
            return {
                "encontrado": False,
                "mensaje": f"No hay experiencia registrada en '{empresa}'. Empresas en el CV: {disponibles}.",
                "_citas": [],
            }
        puestos = filtrados

    return {
        "encontrado": True,
        "anios_totales": cv["perfil"]["anios_experiencia"],
        "puestos": puestos,
        "_citas": [p["id"] for p in puestos],
    }


def obtener_proyectos(tecnologia: str = "") -> dict:
    """Proyectos y contribuciones open source, opcionalmente filtrados por tecnologia."""
    cv = load_cv()
    proyectos = cv["proyectos"]

    if tecnologia:
        terminos = _expandir_consulta(tecnologia)
        proyectos = [p for p in proyectos if _puntuar(p, terminos) > 0]
        if not proyectos:
            return {
                "encontrado": False,
                "mensaje": f"Ningun proyecto del CV usa '{tecnologia}'. No inventes uno.",
                "_citas": [],
            }

    return {
        "encontrado": True,
        "open_source": cv["open_source"],
        "proyectos": proyectos,
        "_citas": [p["id"] for p in proyectos] + ["open-source"],
    }


def obtener_habilidades(categoria: str = "") -> dict:
    """Habilidades tecnicas con el contexto real donde se usaron."""
    cv = load_cv()
    habilidades = cv["habilidades"]

    if categoria:
        terminos = _expandir_consulta(categoria)
        filtradas = [h for h in habilidades if _puntuar(h, terminos) > 0]
        if filtradas:
            habilidades = filtradas

    return {
        "encontrado": True,
        "habilidades": habilidades,
        "certificaciones": cv["certificaciones"],
        "nota": (
            "Cada habilidad trae el campo 'contexto' con el lugar donde se uso de verdad. "
            "Usalo: es la diferencia entre listar tecnologias y explicar experiencia. "
            "Si preguntan por una tecnologia que no aparece aqui, di que no esta en el CV."
        ),
        "_citas": [h["id"] for h in habilidades] + [c["id"] for c in cv["certificaciones"]],
    }


def evaluar_vacante(descripcion_vacante: str) -> dict:
    """Compara el perfil completo contra una vacante, con honestidad obligatoria.

    Esta es la herramienta que hace util al agente para un reclutador: en vez de
    contestar preguntas sueltas, contrasta el CV entero contra un puesto real y
    obliga a nombrar tanto lo que se cumple como lo que no.
    """
    cv = load_cv()
    terminos = _expandir_consulta(descripcion_vacante)

    evidencia: list[dict] = []
    for sec in ("experiencia", "proyectos", "habilidades", "certificaciones", "educacion"):
        for entrada in _entradas_de_seccion(cv, sec):
            if _puntuar(entrada, terminos) > 0:
                evidencia.append({"seccion": sec, "entrada": entrada})

    return {
        "encontrado": True,
        "perfil": cv["perfil"],
        "evidencia_relacionada": evidencia,
        "inventario_completo": {
            "experiencia": [
                {"id": e["id"], "puesto": e["puesto"], "empresa": e["empresa"], "periodo": e["periodo_texto"]}
                for e in cv["experiencia"]
            ],
            "habilidades": {h["categoria"]: h["items"] for h in cv["habilidades"]},
            "certificaciones": [c["nombre"] for c in cv["certificaciones"]],
        },
        "instruccion": (
            "Estructura la respuesta en tres partes: (1) donde SI hay evidencia clara, citando "
            "puesto o proyecto concreto; (2) donde la evidencia es parcial o adyacente, sin "
            "exagerarla; (3) donde NO hay evidencia en el CV, dicho sin rodeos. "
            "Nombrar los huecos es obligatorio: un reclutador confia en el agente que reconoce "
            "lo que le falta al candidato. Nunca conviertas una tecnologia ausente en presente "
            "por parecerse a otra que si esta."
        ),
        "_citas": [
            e["entrada"]["id"]
            for e in evidencia
            if isinstance(e["entrada"], dict) and e["entrada"].get("id")
        ]
        or ["perfil"],
    }


def obtener_contacto() -> dict:
    """Canales de contacto publicos. El telefono nunca se expone."""
    cv = load_cv()
    contacto = {k: v for k, v in cv["contacto"].items() if not k.startswith("_") and k != "id"}
    return {
        "encontrado": True,
        "contacto": contacto,
        "nota": "Solo canales publicos. No hay telefono ni direccion disponibles por diseno.",
        "_citas": ["contacto"],
    }


# --- Registro: definiciones para la API + despachador ------------------------

TOOL_DEFS: list[dict] = [
    {
        "name": "buscar_cv",
        "description": (
            "Busca informacion en el CV por palabra clave. Es la herramienta de proposito "
            "general: usala cuando la pregunta no encaje claramente en una de las herramientas "
            "especificas, o cuando quieras confirmar si un tema existe en el CV antes de responder."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "consulta": {
                    "type": "string",
                    "description": "Terminos a buscar, por ejemplo 'modelos de lenguaje' o 'BigQuery'.",
                },
                "seccion": {
                    "type": "string",
                    "description": "Seccion a la que acotar la busqueda. Cadena vacia para buscar en todo el CV.",
                    "enum": ["", "perfil", "experiencia", "proyectos", "habilidades", "educacion", "certificaciones", "open_source", "preferencias_rol", "intereses", "trayectoria", "forma_de_trabajar", "publicaciones", "formacion_complementaria"],
                },
            },
            "required": ["consulta", "seccion"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "obtener_experiencia",
        "description": (
            "Devuelve la trayectoria laboral completa con logros medibles por puesto. "
            "Usala para preguntas sobre empleos, responsabilidades, antiguedad o resultados."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "empresa": {
                    "type": "string",
                    "description": "Filtra por empresa (GlobalLogic, GTEC, Infosys). Cadena vacia para todas.",
                }
            },
            "required": ["empresa"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "obtener_proyectos",
        "description": (
            "Devuelve proyectos y contribuciones open source. Usala para preguntas sobre "
            "portafolio, contribuciones a Google, o el proyecto mas desafiante."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tecnologia": {
                    "type": "string",
                    "description": "Filtra por tecnologia, por ejemplo 'LookML' o 'BigQuery'. Cadena vacia para todos.",
                }
            },
            "required": ["tecnologia"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "obtener_habilidades",
        "description": (
            "Devuelve las habilidades tecnicas con el contexto donde se usaron realmente, mas "
            "las certificaciones. Usala para '¿que tecnologias dominas?' o '¿sabes X?'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "categoria": {
                    "type": "string",
                    "description": "Filtra por categoria o tecnologia. Cadena vacia para todas.",
                }
            },
            "required": ["categoria"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "evaluar_vacante",
        "description": (
            "Contrasta el perfil completo contra la descripcion de una vacante y devuelve la "
            "evidencia relacionada. Usala cuando peguen una descripcion de puesto o pregunten "
            "si el candidato encaja en un rol concreto."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "descripcion_vacante": {
                    "type": "string",
                    "description": "Texto de la vacante o descripcion del rol a evaluar.",
                }
            },
            "required": ["descripcion_vacante"],
            "additionalProperties": False,
        },
        "strict": True,
    },
    {
        "name": "obtener_contacto",
        "description": (
            "Devuelve los canales de contacto publicos (email, LinkedIn, GitHub, sitio web). "
            "Usala cuando pregunten como contactar a Edher."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        "strict": True,
    },
]

_DESPACHADOR: dict[str, Callable[..., dict]] = {
    "buscar_cv": buscar_cv,
    "obtener_experiencia": obtener_experiencia,
    "obtener_proyectos": obtener_proyectos,
    "obtener_habilidades": obtener_habilidades,
    "evaluar_vacante": evaluar_vacante,
    "obtener_contacto": obtener_contacto,
}


def ejecutar(nombre: str, argumentos: dict) -> dict:
    """Ejecuta una herramienta por nombre. Nunca lanza: los errores se devuelven
    al modelo como resultado para que pueda recuperarse dentro del mismo turno."""
    fn = _DESPACHADOR.get(nombre)
    if fn is None:
        return {"error": f"Herramienta desconocida: {nombre}", "_citas": []}
    try:
        return fn(**argumentos)
    except TypeError as exc:
        return {"error": f"Argumentos invalidos para {nombre}: {exc}", "_citas": []}
    except Exception as exc:  # noqa: BLE001 - el modelo debe ver el fallo, no un 500
        return {"error": f"Fallo al ejecutar {nombre}: {exc}", "_citas": []}
