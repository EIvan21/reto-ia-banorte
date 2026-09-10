"""Configuracion central del agente. Todo se controla por variables de entorno
para que la misma imagen corra en local, en staging y en Cloud Run sin cambios."""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

# Comodidad para desarrollo local: si hay un .env, se carga. En Cloud Run no
# existe el archivo y las variables llegan del entorno y de Secret Manager, asi
# que esto es inerte en produccion. python-dotenv es opcional a proposito: la
# imagen de produccion no lo necesita.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parents[1] / ".env")
except ImportError:
    pass

DATA_DIR = Path(__file__).parent / "data"
CV_PATH = Path(os.getenv("CV_PATH", DATA_DIR / "cv.json"))

# --- Modelo -----------------------------------------------------------------
# Opus 5 con thinking adaptativo. El effort se deja en "low" por defecto porque
# esto es un chat de cara al usuario y la latencia importa mas que la
# profundidad de razonamiento: las respuestas se fundamentan en herramientas,
# no en razonamiento libre. Se sube por env sin recompilar.
MODEL = os.getenv("MODEL", "claude-opus-5")
EFFORT = os.getenv("EFFORT", "low")
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "8000"))
MAX_TOOL_TURNS = int(os.getenv("MAX_TOOL_TURNS", "6"))

# Fallback de servidor ante rechazos por politica: si el modelo declina, la API
# reintenta el mismo request en un modelo de respaldo dentro de la misma llamada.
ENABLE_FALLBACKS = os.getenv("ENABLE_FALLBACKS", "true").lower() == "true"
FALLBACK_BETA = "server-side-fallback-2026-07-01"

# --- Navegacion web ---------------------------------------------------------
# Herramienta del lado del servidor de Anthropic: solo busca URLs que YA estan en
# la conversacion, nunca navega por su cuenta. Sirve para el caso real de pegar
# el enlace de una vacante.
#
# max_uses acota costo y latencia: una respuesta que abre diez paginas no es mas
# util, solo mas lenta. max_content_tokens acota lo que entra al contexto desde
# una fuente que no controlamos.
ENABLE_WEB = os.getenv("ENABLE_WEB", "true").lower() == "true"
WEB_MAX_USOS = int(os.getenv("WEB_MAX_USOS", "3"))
WEB_MAX_TOKENS_CONTENIDO = int(os.getenv("WEB_MAX_TOKENS_CONTENIDO", "20000"))

# --- Reportes descargables --------------------------------------------------
# Sin bucket configurado la capacidad se apaga sola y el agente lo dice, en vez
# de fallar. Es un extra, no el producto.
REPORTES_BUCKET = os.getenv("REPORTES_BUCKET", "").strip()
REPORTES_DIAS_VALIDEZ = int(os.getenv("REPORTES_DIAS_VALIDEZ", "7"))

# --- Seguridad del endpoint -------------------------------------------------
# Bearer opcional. Si AGENT_API_KEY esta definida, /v1/responses la exige.
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "").strip()

# Limite de tamano de entrada, para acotar costo y superficie de abuso.
MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "8000"))

# Ventana de conversacion. El servidor no guarda estado: la plataforma reenvia el
# transcript completo en cada turno, asi que ESTE es el unico control de memoria
# que existe. El tope estaba en 40 mensajes, y estaba mal calibrado: una charla
# real de 36 turnos son 72 mensajes, o sea que se tiraba la mitad de en medio
# EN SILENCIO. El modelo recibia una conversacion que parecia continua y no lo
# era, que es justo la condicion en la que un modelo contesta de memoria en vez
# de volver a consultar el CV.
# El presupuesto de caracteres es el limite que de verdad importa: 40 mensajes
# largos pesan mucho mas que 200 cortos. Con ~13k tokens de CV y una ventana de
# 1M, 240 mensajes / 120k caracteres siguen siendo holgados.
MAX_TURNS_IN_TRANSCRIPT = int(os.getenv("MAX_TURNS_IN_TRANSCRIPT", "240"))
MAX_TRANSCRIPT_CHARS = int(os.getenv("MAX_TRANSCRIPT_CHARS", "120000"))

# --- Telemetria -------------------------------------------------------------
# Sink en BigQuery. Si no hay dataset configurado, la telemetria solo va a
# stdout como JSON estructurado (que Cloud Logging ya indexa por si solo).
BQ_PROJECT = os.getenv("BQ_PROJECT", "").strip()
BQ_DATASET = os.getenv("BQ_DATASET", "").strip()
BQ_TABLE = os.getenv("BQ_TABLE", "agent_events").strip()
TELEMETRY_ENABLED = bool(BQ_PROJECT and BQ_DATASET)

# --- Version desplegada -----------------------------------------------------
# El commit con el que se construyo esta revision. Lo inyecta deploy.sh. Sin
# esto no habia forma de saber que codigo corre en produccion: las revisiones de
# Cloud Run no guardan referencia a git, y el despliegue sube la carpeta local,
# no lo que esta en GitHub. "desconocido" es la respuesta honesta cuando se
# corre fuera de un despliegue (local, pruebas).
GIT_SHA = os.getenv("GIT_SHA", "desconocido")
GIT_LIMPIO = os.getenv("GIT_LIMPIO", "").lower() != "false"


# --- Identidad del agente ---------------------------------------------------
AGENT_NAME = os.getenv("AGENT_NAME", "CV de Edher Diaz")
# Version del agente. La parte semantica se sube a mano cuando cambia lo que el
# agente SABE HACER; el commit se pega automaticamente en el despliegue, para que
# la tarjeta nunca anuncie una version que no corresponde con el codigo que
# corre. Un "1.0.0" fijo no le sirve a nadie: quien lo lee no puede saber si esta
# mirando lo de hoy o lo del primer dia.
_VERSION_BASE = os.getenv("AGENT_VERSION", "1.1.0")
AGENT_VERSION = f"{_VERSION_BASE}+{GIT_SHA}" if GIT_SHA != "desconocido" else _VERSION_BASE

# Nombre de la revision de Cloud Run. La plataforma la inyecta sola; aqui solo se
# lee para poder decir, desde fuera, exactamente que instancia esta respondiendo.
CLOUD_RUN_REVISION = os.getenv("K_REVISION", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")


@lru_cache(maxsize=1)
def load_cv() -> dict:
    """Carga el CV una sola vez por proceso. Es la fuente de verdad del agente."""
    with open(CV_PATH, encoding="utf-8") as f:
        return json.load(f)


def require_api_key() -> str | None:
    """Devuelve la API key de Anthropic o None. No la registra en logs jamas."""
    return os.getenv("ANTHROPIC_API_KEY") or None
