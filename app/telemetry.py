"""Observabilidad del agente.

Dos destinos, uno obligatorio y otro opcional:

  1. stdout como JSON de una sola linea. Cloud Run lo recoge y Cloud Logging lo
     indexa por campo sin configurar nada. Siempre activo, costo cero.
  2. BigQuery, cuando BQ_PROJECT y BQ_DATASET estan definidos. Ahi la telemetria
     se vuelve analizable con SQL y se monitorea con el Agent Analytics Block de
     looker-open-source, del que Edher es autor principal.

El sink de BigQuery es best-effort y corre en un hilo aparte: la observabilidad
nunca debe agregar latencia ni tumbar una respuesta al usuario. Si BigQuery falla,
se registra el fallo en stdout y la conversacion sigue.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .config import BQ_DATASET, BQ_PROJECT, BQ_TABLE, TELEMETRY_ENABLED

# Se configura SOLO nuestro logger, no el raiz. Con basicConfig el logger raiz
# queda en INFO y los clientes HTTP empiezan a escupir una linea por peticion
# ("HTTP Request: POST ... 200 OK"), que ensucia la salida y, en Cloud Run,
# inunda Cloud Logging con ruido que ademas revela el trafico saliente.
_log = logging.getLogger("cv-agent")
if not _log.handlers:
    _manejador = logging.StreamHandler(sys.stdout)
    _manejador.setFormatter(logging.Formatter("%(message)s"))
    _log.addHandler(_manejador)
    _log.setLevel(logging.INFO)
    _log.propagate = False

# Las librerias de red solo hablan cuando algo va mal.
for _ruidoso in ("httpx", "httpcore", "anthropic", "urllib3", "google"):
    logging.getLogger(_ruidoso).setLevel(logging.WARNING)

_ejecutor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="telemetria")
_cliente_bq: Any = None
_candado = threading.Lock()


def _obtener_cliente_bq() -> Any:
    """Crea el cliente de BigQuery de forma perezosa y una sola vez."""
    global _cliente_bq
    if _cliente_bq is not None:
        return _cliente_bq
    with _candado:
        if _cliente_bq is None:
            from google.cloud import bigquery  # import diferido: opcional en local

            _cliente_bq = bigquery.Client(project=BQ_PROJECT)
    return _cliente_bq


def _insertar_en_bq(fila: dict) -> None:
    try:
        cliente = _obtener_cliente_bq()
        destino = f"{BQ_PROJECT}.{BQ_DATASET}.{BQ_TABLE}"
        errores = cliente.insert_rows_json(destino, [fila])
        if errores:
            _log.warning(json.dumps({"severity": "WARNING", "evento": "fallo_insert_bq", "detalle": str(errores)}))
    except Exception as exc:  # noqa: BLE001 - la telemetria jamas rompe la peticion
        _log.warning(json.dumps({"severity": "WARNING", "evento": "excepcion_sink_bq", "detalle": str(exc)}))


def registrar_turno(
    *,
    id_respuesta: str,
    id_conversacion: str,
    modelo: str,
    id_usuario: str = "",
    id_chat: str = "",
    id_respuesta_previa: str = "",
    identidad_de_plataforma: bool = False,
    latencia_ms: int,
    tokens_entrada: int,
    tokens_salida: int,
    tokens_cache_leidos: int = 0,
    tokens_cache_escritos: int = 0,
    herramientas_usadas: list[str],
    citas: list[str],
    etiquetas_guardrail: list[str],
    turnos_herramienta: int,
    streaming: bool,
    categoria: str = "sin_clasificar",
    error: str | None = None,
) -> None:
    """Registra un turno completo de conversacion."""
    fila = {
        "id_evento": str(uuid.uuid4()),
        "marca_tiempo": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "id_respuesta": id_respuesta,
        "id_conversacion": id_conversacion,
        # Identidad que manda la plataforma, si la manda. Cadena vacia cuando no,
        # y nunca inventada: 'identidad_de_plataforma' dice cual de los dos casos
        # es, para que al analizar no se confunda agrupacion con identidad.
        "id_usuario": id_usuario,
        "id_chat": id_chat,
        "id_respuesta_previa": id_respuesta_previa,
        "identidad_de_plataforma": identidad_de_plataforma,
        "modelo": modelo,
        "latencia_ms": latencia_ms,
        "tokens_entrada": tokens_entrada,
        "tokens_salida": tokens_salida,
        # Tokens servidos desde cache y tokens que costo escribirlo. La
        # plataforma reenvia el transcript completo cada turno, asi que sin
        # cache esa conversacion se paga entera una y otra vez.
        "tokens_cache_leidos": tokens_cache_leidos,
        "tokens_cache_escritos": tokens_cache_escritos,
        "herramientas_usadas": herramientas_usadas,
        "num_herramientas": len(herramientas_usadas),
        "citas": citas,
        "num_citas": len(citas),
        "fundamentado": bool(citas),
        "etiquetas_guardrail": etiquetas_guardrail,
        "turnos_herramienta": turnos_herramienta,
        "streaming": streaming,
        "categoria": categoria,
        "error": error,
        "exitoso": error is None,
    }

    # stdout estructurado: Cloud Logging lo parsea a campos automaticamente.
    _log.info(json.dumps({"severity": "INFO", "evento": "turno_completado", **fila}, ensure_ascii=False))

    if TELEMETRY_ENABLED:
        _ejecutor.submit(_insertar_en_bq, fila)


def vaciar(timeout: float = 5.0) -> None:
    """Espera a que salgan las filas en vuelo. Se llama al apagar el servicio.

    El sink corre en un pool de hilos para que la observabilidad no le agregue
    latencia a nadie. El costo es que al apagarse la instancia puede haber filas
    a medio camino: Cloud Run manda SIGTERM y el proceso se va sin esperarlas.

    Perder telemetria al apagar es peor de lo que parece, porque no se pierde al
    azar: se pierde justo la del final -- despliegues, picos, reinicios por
    error. Los momentos sobre los que uno mas quiere mirar los datos despues.
    """
    if not TELEMETRY_ENABLED:
        return
    _ejecutor.shutdown(wait=True, cancel_futures=False)
    _log.info(json.dumps({"severity": "INFO", "evento": "telemetria_vaciada"}))


def registrar(evento: str, **campos: Any) -> None:
    """Log estructurado de proposito general."""
    _log.info(json.dumps({"severity": "INFO", "evento": evento, **campos}, ensure_ascii=False))


def registrar_error(evento: str, **campos: Any) -> None:
    _log.error(json.dumps({"severity": "ERROR", "evento": evento, **campos}, ensure_ascii=False))


# Esquema de la tabla de BigQuery. scripts/setup_bigquery.sh lo usa para crearla.
ESQUEMA_BQ = [
    {"name": "id_evento", "type": "STRING", "mode": "REQUIRED"},
    {"name": "marca_tiempo", "type": "TIMESTAMP", "mode": "REQUIRED"},
    {"name": "id_respuesta", "type": "STRING"},
    {"name": "id_conversacion", "type": "STRING"},
    {"name": "id_usuario", "type": "STRING"},
    {"name": "id_chat", "type": "STRING"},
    {"name": "id_respuesta_previa", "type": "STRING"},
    {"name": "identidad_de_plataforma", "type": "BOOLEAN"},
    {"name": "modelo", "type": "STRING"},
    {"name": "latencia_ms", "type": "INTEGER"},
    {"name": "tokens_entrada", "type": "INTEGER"},
    {"name": "tokens_salida", "type": "INTEGER"},
    {"name": "tokens_cache_leidos", "type": "INTEGER"},
    {"name": "tokens_cache_escritos", "type": "INTEGER"},
    {"name": "herramientas_usadas", "type": "STRING", "mode": "REPEATED"},
    {"name": "num_herramientas", "type": "INTEGER"},
    {"name": "citas", "type": "STRING", "mode": "REPEATED"},
    {"name": "num_citas", "type": "INTEGER"},
    {"name": "fundamentado", "type": "BOOLEAN"},
    {"name": "etiquetas_guardrail", "type": "STRING", "mode": "REPEATED"},
    {"name": "turnos_herramienta", "type": "INTEGER"},
    {"name": "streaming", "type": "BOOLEAN"},
    {"name": "categoria", "type": "STRING"},
    {"name": "error", "type": "STRING"},
    {"name": "exitoso", "type": "BOOLEAN"},
]
