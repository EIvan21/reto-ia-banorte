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


def _texto_de_contenido(contenido: Any) -> str:
    """Extrae texto plano de un campo `content` en cualquiera de sus formas."""
    if isinstance(contenido, str):
        return contenido
    if isinstance(contenido, list):
        partes: list[str] = []
        for parte in contenido:
            if isinstance(parte, str):
                partes.append(parte)
            elif isinstance(parte, dict):
                # input_text | output_text | text
                if isinstance(parte.get("text"), str):
                    partes.append(parte["text"])
        return "\n".join(p for p in partes if p)
    if isinstance(contenido, dict) and isinstance(contenido.get("text"), str):
        return contenido["text"]
    return ""


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

            texto = _texto_de_contenido(item.get("content", ""))
            if not texto:
                continue

            # system/developer del transcript se tratan como contexto de usuario:
            # la autoridad de operador vive en nuestro propio prompt del sistema.
            mensajes.append({"role": "assistant" if rol == "assistant" else "user", "content": texto})

    return mensajes


def fusionar_roles(mensajes: list[dict]) -> list[dict]:
    """Colapsa mensajes consecutivos del mismo rol y garantiza que inicie en 'user'.

    La API de Claude exige que el primer mensaje sea de rol 'user'.
    """
    fusionados: list[dict] = []
    for m in mensajes:
        if fusionados and fusionados[-1]["role"] == m["role"]:
            fusionados[-1]["content"] += "\n\n" + m["content"]
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
