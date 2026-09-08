"""Servidor HTTP compatible con Open Responses.

Endpoints:
  POST /v1/responses                  -- el endpoint del protocolo (streaming y no)
  GET  /.well-known/agent-card.json   -- tarjeta A2A, para el boton "Importar" de la plataforma
  GET  /healthz                       -- sonda de vida para Cloud Run
  GET  /                              -- pagina minima con instrucciones de uso
"""

from __future__ import annotations

import contextlib
import hashlib
import time
from typing import AsyncIterator, Iterator

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import agent, guardrails, openresponses
from .config import (
    AGENT_API_KEY,
    AGENT_NAME,
    AGENT_VERSION,
    MAX_TURNS_IN_TRANSCRIPT,
    MODEL,
    PUBLIC_BASE_URL,
    load_cv,
)
from .telemetry import registrar, registrar_error, registrar_turno

# El servidor MCP es opcional: si el paquete no esta instalado, el agente sigue
# funcionando con el endpoint de Open Responses y solo se anota en el log.
try:
    from .mcp_server import app_asgi as _app_mcp

    APP_MCP = _app_mcp()
except Exception as exc:  # noqa: BLE001
    APP_MCP = None
    _MOTIVO_SIN_MCP = str(exc)


@contextlib.asynccontextmanager
async def ciclo_de_vida(_: FastAPI) -> AsyncIterator[None]:
    """Arranca el gestor de sesiones de MCP junto con la app.

    Al montar una sub-app de Starlette en FastAPI su lifespan NO corre solo, y el
    transporte streamable HTTP de MCP depende de el. Sin esto, /mcp responde 500
    en la primera peticion y el endpoint de Open Responses funciona igual, asi que
    el fallo pasa desapercibido hasta que alguien prueba MCP.
    """
    if APP_MCP is None:
        registrar("mcp_deshabilitado", motivo=_MOTIVO_SIN_MCP)
        yield
        return

    async with APP_MCP.router.lifespan_context(APP_MCP):
        registrar("mcp_habilitado", ruta="/mcp")
        yield


app = FastAPI(
    title=AGENT_NAME, version=AGENT_VERSION, docs_url="/docs", lifespan=ciclo_de_vida
)

if APP_MCP is not None:
    app.mount("/mcp", APP_MCP)

CABECERAS_SSE = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Evita que el proxy de Cloud Run agrupe los eventos y arruine el streaming.
    "X-Accel-Buffering": "no",
}

SUGERENCIAS = [
    "¿Cual ha sido su experiencia con modelos de lenguaje?",
    "Cuentame del proyecto mas desafiante de su carrera",
    "¿Que tecnologias domina y en que contexto las ha usado?",
    "¿Tiene experiencia desplegando en la nube?",
    "Te paso una vacante: ¿que tan bien encaja?",
    "¿Que tipo de rol esta buscando?",
]


# --- Autenticacion ----------------------------------------------------------


def _autorizado(cabecera: str | None) -> bool:
    """Bearer opcional: si no hay AGENT_API_KEY configurada, el endpoint es abierto."""
    if not AGENT_API_KEY:
        return True
    if not cabecera:
        return False
    esperado = f"Bearer {AGENT_API_KEY}"
    # Comparacion en tiempo constante para no filtrar la clave por temporizacion.
    return hashlib.sha256(cabecera.encode()).digest() == hashlib.sha256(esperado.encode()).digest()


def _id_conversacion(mensajes: list[dict]) -> str:
    """Agrupa los turnos de una misma conversacion sin guardar estado.

    Como la plataforma reenvia el transcript completo en cada turno, el primer
    mensaje del usuario es estable durante toda la conversacion: su hash sirve
    como identificador para la telemetria.
    """
    primero = next((m["content"] for m in mensajes if m["role"] == "user"), "")
    return "conv_" + hashlib.sha256(primero.encode("utf-8")).hexdigest()[:16]


# --- Endpoint principal -----------------------------------------------------


@app.post("/v1/responses")
@app.post("/responses")
async def crear_respuesta(request: Request, authorization: str | None = Header(default=None)):
    inicio = time.perf_counter()
    id_respuesta = openresponses.nuevo_id_respuesta()
    id_mensaje = openresponses.nuevo_id_mensaje()

    if not _autorizado(authorization):
        registrar("auth_rechazada")
        return JSONResponse(
            status_code=401,
            content=openresponses.construir_error(
                id_respuesta, MODEL, "Falta o es invalido el token de portador.", "unauthorized"
            ),
        )

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content=openresponses.construir_error(
                id_respuesta, MODEL, "El cuerpo debe ser JSON valido.", "invalid_request"
            ),
        )

    mensajes = openresponses.fusionar_roles(openresponses.parsear_entrada(payload))
    if not mensajes:
        return JSONResponse(
            status_code=400,
            content=openresponses.construir_error(
                id_respuesta, MODEL, "No se encontro ningun mensaje de usuario en 'input'.", "invalid_request"
            ),
        )

    # Acota transcripts muy largos conservando el inicio (que ancla el tema) y la
    # cola reciente (que lleva el hilo de la conversacion).
    if len(mensajes) > MAX_TURNS_IN_TRANSCRIPT:
        mensajes = mensajes[:2] + mensajes[-(MAX_TURNS_IN_TRANSCRIPT - 2):]

    instrucciones = payload.get("instructions") or ""
    streaming = bool(payload.get("stream", False))
    id_conv = _id_conversacion(mensajes)
    ultimo_usuario = next((m["content"] for m in reversed(mensajes) if m["role"] == "user"), "")

    # --- Guardrail de entrada: puede cortar antes de gastar una llamada al modelo
    veredicto = guardrails.revisar_entrada(ultimo_usuario)
    if not veredicto.permitido:
        registrar("guardrail_entrada_bloqueo", motivo=veredicto.motivo, id_conversacion=id_conv)
        registrar_turno(
            id_respuesta=id_respuesta,
            id_conversacion=id_conv,
            modelo=MODEL,
            latencia_ms=int((time.perf_counter() - inicio) * 1000),
            tokens_entrada=0,
            tokens_salida=0,
            herramientas_usadas=[],
            citas=[],
            etiquetas_guardrail=veredicto.etiquetas,
            turnos_herramienta=0,
            streaming=streaming,
        )
        if streaming:
            return StreamingResponse(
                _stream_texto_fijo(id_respuesta, id_mensaje, veredicto.respuesta_segura),
                media_type="text/event-stream",
                headers=CABECERAS_SSE,
            )
        return JSONResponse(
            content=openresponses.construir_respuesta(
                id_respuesta=id_respuesta,
                id_mensaje=id_mensaje,
                modelo=MODEL,
                texto=veredicto.respuesta_segura,
                tokens_entrada=0,
                tokens_salida=0,
            )
        )

    if streaming:
        return StreamingResponse(
            _stream_agente(id_respuesta, id_mensaje, mensajes, instrucciones, id_conv, inicio),
            media_type="text/event-stream",
            headers=CABECERAS_SSE,
        )

    return JSONResponse(
        content=_responder_completo(id_respuesta, id_mensaje, mensajes, instrucciones, id_conv, inicio)
    )


def _stream_texto_fijo(id_respuesta: str, id_mensaje: str, texto: str) -> Iterator[str]:
    """Emite un texto ya resuelto como stream SSE valido (respuestas de guardrail)."""
    emisor = openresponses.EmisorSSE(id_respuesta, id_mensaje, MODEL)
    yield from emisor.inicio()
    yield emisor.delta(texto)
    yield from emisor.fin(0, 0)


def _stream_agente(
    id_respuesta: str,
    id_mensaje: str,
    mensajes: list[dict],
    instrucciones: str,
    id_conv: str,
    inicio: float,
) -> Iterator[str]:
    emisor = openresponses.EmisorSSE(id_respuesta, id_mensaje, MODEL)
    yield from emisor.inicio()

    resumen = None
    try:
        for tipo, carga in agent.responder(mensajes, instrucciones):
            if tipo == "delta":
                yield emisor.delta(str(carga))
            else:
                resumen = carga
    except Exception as exc:  # noqa: BLE001
        registrar_error("fallo_stream", detalle=str(exc), id_conversacion=id_conv)
        yield from emisor.error("El agente fallo a mitad de la respuesta.")
        return

    etiquetas: list[str] = []
    if resumen is not None:
        etiquetas = guardrails.verificar_fundamento(resumen.texto, resumen.citas)

    yield from emisor.fin(
        resumen.tokens_entrada if resumen else 0,
        resumen.tokens_salida if resumen else 0,
    )

    registrar_turno(
        id_respuesta=id_respuesta,
        id_conversacion=id_conv,
        modelo=MODEL,
        latencia_ms=int((time.perf_counter() - inicio) * 1000),
        tokens_entrada=resumen.tokens_entrada if resumen else 0,
        tokens_salida=resumen.tokens_salida if resumen else 0,
        herramientas_usadas=resumen.herramientas_usadas if resumen else [],
        citas=resumen.citas if resumen else [],
        etiquetas_guardrail=etiquetas,
        turnos_herramienta=resumen.turnos_herramienta if resumen else 0,
        streaming=True,
        error=resumen.error if resumen else "sin_resumen",
    )


def _responder_completo(
    id_respuesta: str,
    id_mensaje: str,
    mensajes: list[dict],
    instrucciones: str,
    id_conv: str,
    inicio: float,
) -> dict:
    resumen = None
    for tipo, carga in agent.responder(mensajes, instrucciones):
        if tipo == "fin":
            resumen = carga

    texto = resumen.texto if resumen else ""
    texto, etiquetas_pii = guardrails.redactar_pii(texto)
    etiquetas = etiquetas_pii + guardrails.verificar_fundamento(texto, resumen.citas if resumen else [])

    registrar_turno(
        id_respuesta=id_respuesta,
        id_conversacion=id_conv,
        modelo=MODEL,
        latencia_ms=int((time.perf_counter() - inicio) * 1000),
        tokens_entrada=resumen.tokens_entrada if resumen else 0,
        tokens_salida=resumen.tokens_salida if resumen else 0,
        herramientas_usadas=resumen.herramientas_usadas if resumen else [],
        citas=resumen.citas if resumen else [],
        etiquetas_guardrail=etiquetas,
        turnos_herramienta=resumen.turnos_herramienta if resumen else 0,
        streaming=False,
        error=resumen.error if resumen else "sin_resumen",
    )

    return openresponses.construir_respuesta(
        id_respuesta=id_respuesta,
        id_mensaje=id_mensaje,
        modelo=MODEL,
        texto=texto,
        tokens_entrada=resumen.tokens_entrada if resumen else 0,
        tokens_salida=resumen.tokens_salida if resumen else 0,
    )


# --- Metadatos --------------------------------------------------------------


@app.get("/.well-known/agent-card.json")
async def tarjeta_agente():
    """Tarjeta A2A. Permite registrar el agente con 'Importar desde tarjeta de agente'."""
    cv = load_cv()
    return JSONResponse(
        content={
            "name": AGENT_NAME,
            "description": (
                f"Agente conversacional del CV de {cv['perfil']['nombre']}, "
                f"{cv['perfil']['titular']} especializado en {cv['perfil']['especialidad']}. "
                "Responde sobre perfil, experiencia, habilidades y proyectos, fundamentado "
                "en un CV estructurado y sin inventar datos."
            ),
            "version": AGENT_VERSION,
            "capabilities": {"streaming": True},
            "defaultInputModes": ["text/plain"],
            "defaultOutputModes": ["text/plain"],
            "promptSuggestions": SUGERENCIAS,
            "skills": [
                {
                    "id": "consultar-cv",
                    "name": "Consultar el CV",
                    "description": "Responde sobre experiencia, habilidades, proyectos, estudios y certificaciones.",
                },
                {
                    "id": "evaluar-vacante",
                    "name": "Contrastar contra una vacante",
                    "description": "Recibe una descripcion de puesto y devuelve fortalezas, coincidencias parciales y huecos reales.",
                },
            ],
            "supportedInterfaces": [
                {
                    "url": f"{PUBLIC_BASE_URL}/v1",
                    "protocolBinding": "https://openresponses.org/v1",
                    "protocolVersion": "1.0",
                },
                # Las mismas herramientas por MCP. La plataforma del reto usa la
                # interfaz de arriba; esta permite que cualquier otro agente
                # consulte el CV directamente.
                {
                    "url": f"{PUBLIC_BASE_URL}/mcp",
                    "protocolBinding": "https://modelcontextprotocol.io",
                    "protocolVersion": "2025-06-18",
                },
            ],
        }
    )


@app.get("/healthz")
async def salud():
    """Sonda de vida. Valida que el CV cargue: sin el, el agente no sirve de nada."""
    try:
        cv = load_cv()
        return {
            "estado": "ok",
            "modelo": MODEL,
            "cv_version": cv["_meta"]["version"],
            "entradas_cv": {
                "experiencia": len(cv["experiencia"]),
                "proyectos": len(cv["proyectos"]),
                "habilidades": len(cv["habilidades"]),
            },
        }
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=503, content={"estado": "degradado", "detalle": str(exc)})


@app.get("/", response_class=HTMLResponse)
async def inicio():
    cv = load_cv()
    sugerencias = "".join(f"<li>{s}</li>" for s in SUGERENCIAS)
    return f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{AGENT_NAME}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font: 16px/1.6 system-ui, -apple-system, sans-serif; max-width: 46rem;
         margin: 3rem auto; padding: 0 1.25rem; }}
  code {{ background: color-mix(in srgb, currentColor 10%, transparent);
          padding: .15em .4em; border-radius: 4px; font-size: .9em; }}
  pre {{ background: color-mix(in srgb, currentColor 8%, transparent);
         padding: 1rem; border-radius: 8px; overflow-x: auto; }}
  .sub {{ opacity: .7; }}
</style></head><body>
<h1>{AGENT_NAME}</h1>
<p class="sub">Agente conversacional compatible con Open Responses sobre el CV de
{cv['perfil']['nombre']} &mdash; {cv['perfil']['titular']}.</p>
<h2>Dos protocolos, las mismas herramientas</h2>
<pre>POST {PUBLIC_BASE_URL}/v1/responses   &larr; Open Responses
POST {PUBLIC_BASE_URL}/mcp            &larr; MCP (streamable HTTP)</pre>
<p>Registrable en la plataforma con la tarjeta de agente en
<code>/.well-known/agent-card.json</code>.</p>
<h2>Que preguntarle</h2>
<ul>{sugerencias}</ul>
<h2>Probar desde la terminal</h2>
<pre>curl -N -X POST {PUBLIC_BASE_URL}/v1/responses \\
  -H "Content-Type: application/json" \\
  -d '{{"input":"¿Que experiencia tiene con modelos de lenguaje?","stream":true}}'</pre>
</body></html>"""
