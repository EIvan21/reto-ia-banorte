"""Capa del protocolo Open Responses.

La forma exacta de la carga util se verifico contra el agente de referencia del
Reto IA Banorte (el agente "Guia del reto"), no solo contra la especificacion
escrita. Campos, orden de eventos y el terminador `data: [DONE]` replican lo que
ese endpoint emite realmente, que es contra lo que la plataforma va a validar.

Secuencia de eventos SSE observada:
    response.created -> response.in_progress -> response.output_item.added ->
    response.content_part.added -> response.output_text.delta (N veces) ->
    response.output_text.done -> response.content_part.done ->
    response.output_item.done -> response.completed -> data: [DONE]
"""

from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any, Iterator

_ALFABETO = "abcdefghijklmnopqrstuvwxyz0123456789"


def _id(prefijo: str) -> str:
    return prefijo + "".join(secrets.choice(_ALFABETO) for _ in range(24))


def nuevo_id_respuesta() -> str:
    return _id("resp_")


def nuevo_id_mensaje() -> str:
    return _id("msg_")


# --- Entrada ----------------------------------------------------------------


_PREFIJO_DATOS = re.compile(r"^data:(image/(?:png|jpe?g|gif|webp));base64,(.+)$", re.I | re.S)

# Tope por imagen. Una captura de pantalla normal ronda 1-3 MB; mas alla de esto
# es casi seguro un envio accidental o un intento de agotar memoria.
_MAX_BYTES_IMAGEN = 5 * 1024 * 1024


def _bloque_de_imagen(url: str) -> dict | None:
    """Convierte una imagen de Open Responses a un bloque de contenido de Claude.

    Se aceptan dos formas: data URI en base64 (lo que manda un cliente que sube
    un archivo) y URL http(s) (lo que manda uno que entrega por enlace, como la
    opcion "URL de capacidad" de la plataforma).
    """
    if not isinstance(url, str) or not url:
        return None

    coincidencia = _PREFIJO_DATOS.match(url.strip())
    if coincidencia:
        tipo, datos = coincidencia.group(1).lower(), coincidencia.group(2)
        # El tamano en base64 es ~4/3 del binario; se acota antes de decodificar.
        if len(datos) * 3 // 4 > _MAX_BYTES_IMAGEN:
            return None
        if tipo == "image/jpg":
            tipo = "image/jpeg"
        return {"type": "image", "source": {"type": "base64", "media_type": tipo, "data": datos}}

    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}

    return None


def _bloques_de_contenido(contenido: Any) -> list[dict]:
    """Normaliza un campo `content` a bloques de contenido de Claude.

    Devuelve texto e imagenes. Todo lo que no se reconozca se ignora en silencio:
    ser liberal en lo que se acepta evita romper con clientes que manden campos
    que no conocemos.
    """
    if isinstance(contenido, str):
        return [{"type": "text", "text": contenido}] if contenido else []

    partes = contenido if isinstance(contenido, list) else [contenido]
    bloques: list[dict] = []
    for parte in partes:
        if isinstance(parte, str):
            if parte:
                bloques.append({"type": "text", "text": parte})
            continue
        if not isinstance(parte, dict):
            continue

        tipo = parte.get("type", "")
        if tipo in ("input_image", "image", "output_image"):
            url = parte.get("image_url") or parte.get("url") or ""
            if isinstance(url, dict):  # algunos clientes anidan {"url": ...}
                url = url.get("url", "")
            bloque = _bloque_de_imagen(url)
            if bloque:
                bloques.append(bloque)
        elif isinstance(parte.get("text"), str) and parte["text"]:
            bloques.append({"type": "text", "text": parte["text"]})

    return bloques


def _texto_de_contenido(contenido: Any) -> str:
    """Extrae solo el texto de un campo `content`, ignorando imagenes."""
    return "\n".join(b["text"] for b in _bloques_de_contenido(contenido) if b["type"] == "text")


def parsear_entrada(payload: dict) -> list[dict]:
    """Normaliza `input` a una lista [{'role': 'user'|'assistant', 'content': str}].

    Se acepta tanto la forma corta (`"input": "hola"`) como la lista de items de
    mensaje, porque distintos clientes de Open Responses mandan ambas. Se es
    liberal en lo que se recibe y estricto en lo que se emite.
    """
    entrada = payload.get("input")

    if isinstance(entrada, str):
        return [{"role": "user", "content": entrada}]

    mensajes: list[dict] = []
    if isinstance(entrada, list):
        for item in entrada:
            if not isinstance(item, dict):
                if isinstance(item, str):
                    mensajes.append({"role": "user", "content": item})
                continue

            # Se ignoran items que no son mensajes (llamadas a herramientas del
            # cliente, razonamiento, etc.): este agente resuelve sus herramientas
            # del lado del servidor.
            tipo = item.get("type", "message")
            if tipo not in ("message", None):
                continue

            rol = item.get("role", "user")
            if rol not in ("user", "assistant", "system", "developer"):
                continue

            bloques = _bloques_de_contenido(item.get("content", ""))
            if not bloques:
                continue

            # system/developer del transcript se tratan como contexto de usuario:
            # la autoridad de operador vive en nuestro propio prompt del sistema.
            rol_normalizado = "assistant" if rol == "assistant" else "user"

            # Las imagenes solo tienen sentido en turnos del usuario. En un turno
            # del asistente se descartan: reenviarlas como si el modelo las
            # hubiera producido ensucia el historial.
            if rol_normalizado == "assistant":
                bloques = [b for b in bloques if b["type"] == "text"]
                if not bloques:
                    continue

            # Solo texto -> cadena simple, que es el caso comun y el mas barato.
            if all(b["type"] == "text" for b in bloques):
                mensajes.append({
                    "role": rol_normalizado,
                    "content": "\n".join(b["text"] for b in bloques),
                })
            else:
                mensajes.append({"role": rol_normalizado, "content": bloques})

    return mensajes


def fusionar_roles(mensajes: list[dict]) -> list[dict]:
    """Colapsa mensajes consecutivos del mismo rol y garantiza que inicie en 'user'.

    La API de Claude exige que el primer mensaje sea de rol 'user'.
    """
    def _como_bloques(contenido: Any) -> list[dict]:
        return [{"type": "text", "text": contenido}] if isinstance(contenido, str) else list(contenido)

    fusionados: list[dict] = []
    for m in mensajes:
        if fusionados and fusionados[-1]["role"] == m["role"]:
            previo, actual = fusionados[-1]["content"], m["content"]
            if isinstance(previo, str) and isinstance(actual, str):
                fusionados[-1]["content"] = previo + "\n\n" + actual
            else:
                # Con imagenes de por medio no se puede concatenar texto plano:
                # se fusionan como listas de bloques.
                fusionados[-1]["content"] = _como_bloques(previo) + _como_bloques(actual)
        else:
            fusionados.append(dict(m))

    while fusionados and fusionados[0]["role"] != "user":
        fusionados.pop(0)

    return fusionados


# --- Salida no-streaming ----------------------------------------------------


def construir_respuesta(
    *,
    id_respuesta: str,
    id_mensaje: str,
    modelo: str,
    texto: str,
    tokens_entrada: int,
    tokens_salida: int,
    creado_en: int | None = None,
) -> dict:
    creado = creado_en or int(time.time())
    return {
        "id": id_respuesta,
        "object": "response",
        "model": modelo,
        "created_at": creado,
        "status": "completed",
        "completed_at": int(time.time()),
        "output": [
            {
                "id": id_mensaje,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "annotations": [], "text": texto}],
            }
        ],
        "usage": {
            "input_tokens": tokens_entrada,
            "output_tokens": tokens_salida,
            "total_tokens": tokens_entrada + tokens_salida,
        },
        # `error` va como null explicito, no omitido: el cliente lo lee siempre.
        "error": None,
    }


def construir_error(id_respuesta: str, modelo: str, mensaje: str, codigo: str = "server_error") -> dict:
    ahora = int(time.time())
    return {
        "id": id_respuesta,
        "object": "response",
        "model": modelo,
        "created_at": ahora,
        "status": "failed",
        "completed_at": ahora,
        "output": [],
        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "error": {"code": codigo, "message": mensaje},
    }


# --- Salida streaming (SSE) -------------------------------------------------


class EmisorSSE:
    """Construye la secuencia de eventos SSE con numeracion correlativa.

    La especificacion exige que el campo `event:` coincida con el `type` del
    cuerpo, y que el terminador sea literalmente `data: [DONE]` (sin linea
    `event:`, tal como lo emite el agente de referencia).
    """

    def __init__(self, id_respuesta: str, id_mensaje: str, modelo: str) -> None:
        self.id_respuesta = id_respuesta
        self.id_mensaje = id_mensaje
        self.modelo = modelo
        self.creado_en = int(time.time())
        self._secuencia = 0
        self._texto: list[str] = []

    def _evento(self, tipo: str, cuerpo: dict) -> str:
        cuerpo = {"type": tipo, **cuerpo, "sequence_number": self._secuencia}
        self._secuencia += 1
        return f"event: {tipo}\ndata: {json.dumps(cuerpo, ensure_ascii=False)}\n\n"

    def _sobre_respuesta(self, estado: str, salida: list[dict] | None = None, usage: dict | None = None) -> dict:
        sobre: dict[str, Any] = {
            "id": self.id_respuesta,
            "object": "response",
            "model": self.modelo,
            "created_at": self.creado_en,
            "status": estado,
            "output": salida if salida is not None else [],
        }
        if estado == "completed":
            sobre["completed_at"] = int(time.time())
            sobre["usage"] = usage or {}
            sobre["error"] = None
        return sobre

    # -- fases del stream --

    def inicio(self) -> Iterator[str]:
        yield self._evento("response.created", {"response": self._sobre_respuesta("queued")})
        yield self._evento("response.in_progress", {"response": self._sobre_respuesta("in_progress")})
        yield self._evento(
            "response.output_item.added",
            {
                "output_index": 0,
                "item": {
                    "id": self.id_mensaje,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
            },
        )
        yield self._evento(
            "response.content_part.added",
            {
                "item_id": self.id_mensaje,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "annotations": [], "text": ""},
            },
        )

    def delta(self, fragmento: str) -> str:
        self._texto.append(fragmento)
        return self._evento(
            "response.output_text.delta",
            {"item_id": self.id_mensaje, "output_index": 0, "content_index": 0, "delta": fragmento},
        )

    def fin(self, tokens_entrada: int, tokens_salida: int) -> Iterator[str]:
        texto = "".join(self._texto)
        contenido = [{"type": "output_text", "annotations": [], "text": texto}]
        item = {
            "id": self.id_mensaje,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": contenido,
        }
        usage = {
            "input_tokens": tokens_entrada,
            "output_tokens": tokens_salida,
            "total_tokens": tokens_entrada + tokens_salida,
        }

        yield self._evento(
            "response.output_text.done",
            {"item_id": self.id_mensaje, "output_index": 0, "content_index": 0, "text": texto},
        )
        yield self._evento(
            "response.content_part.done",
            {
                "item_id": self.id_mensaje,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "annotations": [], "text": texto},
            },
        )
        yield self._evento("response.output_item.done", {"output_index": 0, "item": item})
        yield self._evento(
            "response.completed", {"response": self._sobre_respuesta("completed", [item], usage)}
        )
        yield "data: [DONE]\n\n"

    def error(self, mensaje: str) -> Iterator[str]:
        """Emite un fallo a mitad de stream sin dejar la conexion colgada."""
        yield self._evento(
            "response.failed",
            {
                "response": {
                    **self._sobre_respuesta("failed"),
                    "error": {"code": "server_error", "message": mensaje},
                }
            },
        )
        yield "data: [DONE]\n\n"

    @property
    def texto_acumulado(self) -> str:
        return "".join(self._texto)
