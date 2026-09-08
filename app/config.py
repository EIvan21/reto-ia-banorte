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

# --- Seguridad del endpoint -------------------------------------------------
# Bearer opcional. Si AGENT_API_KEY esta definida, /v1/responses la exige.
AGENT_API_KEY = os.getenv("AGENT_API_KEY", "").strip()

# Limite de tamano de entrada, para acotar costo y superficie de abuso.
MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "8000"))
MAX_TURNS_IN_TRANSCRIPT = int(os.getenv("MAX_TURNS_IN_TRANSCRIPT", "40"))

# --- Telemetria -------------------------------------------------------------
# Sink en BigQuery. Si no hay dataset configurado, la telemetria solo va a
# stdout como JSON estructurado (que Cloud Logging ya indexa por si solo).
BQ_PROJECT = os.getenv("BQ_PROJECT", "").strip()
BQ_DATASET = os.getenv("BQ_DATASET", "").strip()
BQ_TABLE = os.getenv("BQ_TABLE", "agent_events").strip()
TELEMETRY_ENABLED = bool(BQ_PROJECT and BQ_DATASET)

# --- Identidad del agente ---------------------------------------------------
AGENT_NAME = os.getenv("AGENT_NAME", "CV de Edher Diaz")
AGENT_VERSION = "1.0.0"
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")


@lru_cache(maxsize=1)
def load_cv() -> dict:
    """Carga el CV una sola vez por proceso. Es la fuente de verdad del agente."""
    with open(CV_PATH, encoding="utf-8") as f:
        return json.load(f)


def require_api_key() -> str | None:
    """Devuelve la API key de Anthropic o None. No la registra en logs jamas."""
    return os.getenv("ANTHROPIC_API_KEY") or None
