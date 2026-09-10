"""Servidor HTTP compatible con Open Responses.

Endpoints:
  POST /v1/responses                  -- el endpoint del protocolo (streaming y no)
  GET  /.well-known/agent-card.json   -- tarjeta A2A, para el boton "Importar" de la plataforma
  GET  /salud                         -- sonda de vida (NO /healthz: Google la intercepta)
  GET  /                              -- pagina minima con instrucciones de uso
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import hmac
import time
from typing import Any, AsyncIterator, Iterator

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import agent, categorias, guardrails, openresponses
from .config import (
    AGENT_API_KEY,
    AGENT_NAME,
    AGENT_VERSION,
    CLOUD_RUN_REVISION,
    GIT_LIMPIO,
    GIT_SHA,
    MAX_BODY_BYTES,
    MAX_TRANSCRIPT_CHARS,
    MAX_TURNS_IN_TRANSCRIPT,
    MODEL,
    PUBLIC_BASE_URL,
    load_cv,
)
from .telemetry import registrar, registrar_error, registrar_turno, vaciar

# El servidor MCP es opcional: si el paquete no esta instalado, el agente sigue
# funcionando con el endpoint de Open Responses y solo se anota en el log.
try:
    from .mcp_server import app_asgi as _app_mcp

    APP_MCP = _app_mcp()
except Exception as exc:  # noqa: BLE001
    APP_MCP = None
    _MOTIVO_SIN_MCP = str(exc)


# --- Ventana de conversacion ------------------------------------------------

AVISO_DE_CORTE = (
    "[Nota del sistema: esta conversacion es larga y se omitieron {n} mensajes "
    "intermedios. NO supongas que un dato ya se reviso antes. Si te preguntan "
    "algo concreto del CV, vuelve a consultarlo con las herramientas aunque "
    "sientas que ya lo respondiste.]"
)


def _peso(mensaje: dict) -> int:
    """Cuanto ocupa un mensaje, en caracteres.

    len(content) cuenta BLOQUES cuando el contenido es una lista, no caracteres.
    Un mensaje con una imagen en base64 de 60k caracteres contaba como 2, asi
    que el presupuesto del transcript no acotaba nada en cuanto habia imagenes.
    """
    contenido = mensaje.get("content")
    if isinstance(contenido, str):
        return len(contenido)
    if isinstance(contenido, list):
        total = 0
        for bloque in contenido:
            if not isinstance(bloque, dict):
                total += len(str(bloque))
                continue
            if bloque.get("type") == "text":
                total += len(bloque.get("text") or "")
            else:
                # Las imagenes pesan por sus datos, que es lo que de verdad
                # viaja al modelo.
                fuente = bloque.get("source") or {}
                total += len(str(fuente.get("data") or fuente.get("url") or ""))
        return total
    return len(str(contenido or ""))


def acotar_transcript(mensajes: list[dict]) -> tuple[list[dict], int]:
    """Recorta un transcript largo y DEJA CONSTANCIA del recorte.

    El servidor es sin estado: la plataforma reenvia la conversacion entera cada
    turno, asi que recortar aqui es lo unico que decide que recuerda el agente.

    Lo que se conserva: los 2 primeros mensajes (anclan de que va la charla) y
    la cola mas reciente (lleva el hilo). Lo que cambia respecto a la version
    anterior es que el corte deja de ser invisible. Antes se empalmaban el
    principio y el final sin marca alguna, y el modelo recibia una conversacion
    aparentemente continua con un hueco adentro. Esa es exactamente la situacion
    en la que un modelo responde de memoria -- "esto ya lo dijimos" -- en vez de
    volver a consultar el CV, y de ahi salen los datos inventados.

    Ahora se inserta un aviso explicito en el lugar del hueco, y ademas se anota
    en la telemetria para que el corte sea visible al operar, no solo al fallar.
    """
    total = len(mensajes)
    cabeza = 2

    if total > MAX_TURNS_IN_TRANSCRIPT:
        cola = MAX_TURNS_IN_TRANSCRIPT - cabeza
    else:
        cola = total - cabeza

    # Presupuesto de caracteres: manda sobre el conteo de mensajes, porque unos
    # pocos mensajes enormes (alguien pegando una vacante completa) pesan mas
    # que muchos cortos.
    #
    # Tiene un piso: nunca baja de 2 mensajes de cabeza mas 2 de cola, porque
    # sin el ultimo no hay nada que responder. Si esos cuatro ya superan el
    # presupuesto -- cuatro mensajes con imagenes grandes, por ejemplo -- el
    # tope duro no es este sino MAX_BODY_BYTES, que corta la peticion antes de
    # parsearla.
    while cola > 2:
        recorte = mensajes[:cabeza] + mensajes[-cola:] if cola < total else mensajes
        if sum(_peso(m) for m in recorte) <= MAX_TRANSCRIPT_CHARS:
            break
        cola -= 2

    if cola >= total - cabeza:
        return mensajes, 0

    omitidos = total - cabeza - cola
    aviso = {"role": "user", "content": AVISO_DE_CORTE.format(n=omitidos)}
    return mensajes[:cabeza] + [aviso] + mensajes[-cola:], omitidos


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
        try:
            yield
        finally:
            vaciar()
        return

    async with APP_MCP.router.lifespan_context(APP_MCP):
        registrar("mcp_habilitado", ruta="/mcp")
        try:
            yield
        finally:
            # Cloud Run manda SIGTERM y el proceso se va. Sin esto quedan filas
            # de telemetria a medio camino, y no se pierden al azar: se pierde
            # justo la del final -- despliegues, picos, reinicios por error.
            vaciar()


app = FastAPI(
    title=AGENT_NAME, version=AGENT_VERSION, docs_url="/docs", lifespan=ciclo_de_vida
)

if APP_MCP is not None:
    # Nota de seguridad: /mcp queda abierto aunque AGENT_API_KEY este configurada.
    # El Bearer protege /v1/responses porque ahi cada peticion gasta una llamada
    # al modelo, y sin auth cualquiera que descubra la URL consume la API key.
    # /mcp no llama al modelo: solo lee un CV que de todos modos es publico, con
    # costo cero por peticion. Cerrarlo no protegeria nada y romperia el objetivo
    # de que cualquier agente pueda consultar el perfil.
    app.mount("/mcp", APP_MCP)

CABECERAS_SSE = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    # Evita que el proxy de Cloud Run agrupe los eventos y arruine el streaming.
    "X-Accel-Buffering": "no",
}

# Sugerencias que aparecen sobre el cuadro de texto al seleccionar el agente.
# Son el escaparate: la mayoria de quien evalua hace clic en una en vez de
# escribir, asi que cada una tiene que mostrar algo distinto.
#
# Las dos ultimas son deliberadas y no obvias:
#   - Kubernetes parece autodestructiva y es lo contrario: demuestra en vivo que
#     el agente reconoce lo que NO sabe en vez de inflarlo. Es el argumento de
#     confiabilidad, y se ve mejor demostrado que explicado.
#   - La de arquitectura lleva al evaluador justo al tema que vino a evaluar.
SUGERENCIAS = [
    "¿Cual ha sido su experiencia con modelos de lenguaje?",
    "Cuentame del proyecto mas desafiante de su carrera",
    "¿Que tecnologias domina y en que contexto las ha usado?",
    "Te paso una vacante: ¿que tan bien encaja?",
    "¿Que tipo de rol esta buscando?",
    "¿Tiene experiencia con Kubernetes?",
    "¿Como esta construido este agente?",
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
    # Comparar dos digests del mismo largo ya era constante en la practica, pero
    # compare_digest es la primitiva que existe para esto: no depende de que el
    # de al lado razone sobre por que el == de bytes no delata nada aqui.
    return hmac.compare_digest(cabecera.encode(), esperado.encode())


def _ultima_pregunta(mensajes: list[dict]) -> str:
    """Texto del ultimo turno del usuario. Solo se usa para clasificar en memoria:
    nunca se guarda ni se registra."""
    for m in reversed(mensajes):
        if m.get("role") == "user":
            return guardrails.texto_plano(m.get("content"))
    return ""


# Campos de identidad del protocolo. Son opcionales: no toda plataforma los
# manda, asi que se leen si vienen y se ignoran si no. Leerlos no cuesta nada y
# no leerlos costaba una metrica mal agrupada.
def _effort_pedido(payload: dict) -> str:
    """Esfuerzo de razonamiento que pide quien llama, si pide uno valido.

    Opus 5 quito temperature, top_p y top_k, asi que effort es el unico control
    de profundidad que el modelo todavia expone. Ignorarlo obligaba a
    redesplegar el servicio para probar el agente con mas profundidad.

    Se aceptan las dos formas en que llega segun el cliente: anidada bajo
    'reasoning' (la que sugiere la plataforma en su campo de parametros extra)
    y suelta en la raiz. Lo que no sea un nivel permitido se ignora en silencio
    y se usa el de la configuracion: un valor invalido tumbaria la peticion con
    un 400 de la API, y el resto del mensaje si es atendible.
    """
    razonamiento = payload.get("reasoning")
    candidatos = [payload.get("effort")]
    if isinstance(razonamiento, dict):
        candidatos.insert(0, razonamiento.get("effort"))

    for c in candidatos:
        valido = agent.normalizar_effort(c)
        if valido:
            return valido
    return ""


def _ids_de_la_plataforma(payload: dict) -> dict[str, str]:
    """Extrae los identificadores que la plataforma nos da, si nos da alguno.

    Nunca se inventan. Si la plataforma no manda identidad, aqui no hay identidad:
    hashear el primer mensaje da agrupacion, no identidad, y confundir las dos es
    como se acaba creyendo que se tiene memoria por usuario cuando no se tiene.
    """
    metadatos = payload.get("metadata")
    if not isinstance(metadatos, dict):
        metadatos = {}

    def _texto(*candidatos: Any) -> str:
        for c in candidatos:
            if isinstance(c, str) and c.strip():
                return c.strip()[:128]
            if isinstance(c, dict):
                for llave in ("id", "user_id", "conversation_id"):
                    v = c.get(llave)
                    if isinstance(v, str) and v.strip():
                        return v.strip()[:128]
        return ""

    return {
        "id_usuario": _texto(payload.get("user"), metadatos.get("user_id"),
                             metadatos.get("usuario")),
        "id_chat": _texto(payload.get("conversation"), metadatos.get("conversation_id"),
                          metadatos.get("chat_id"), metadatos.get("thread_id")),
        "id_respuesta_previa": _texto(payload.get("previous_response_id")),
    }


def _id_conversacion(mensajes: list[dict], id_chat: str = "") -> str:
    """Agrupa los turnos de una misma conversacion sin guardar estado.

    Si la plataforma manda un id de conversacion, ese manda: es identidad real.

    Si no, se deriva del primer mensaje del usuario, que la plataforma reenvia
    intacto en cada turno y por tanto es estable durante toda la conversacion.

    Esa derivacion tiene una colision conocida y no se puede quitar sin romper
    algo peor: dos personas que empiecen con "Hola" caen en el mismo grupo.
    Probe mezclar tambien la primera respuesta del agente, que es mucho mas
    distintiva -- y el resultado fue peor: en el turno 1 esa respuesta todavia
    no existe, asi que el primer turno de cada conversacion se iba a un id
    aparte y se rompia el hilo, que es justo para lo que sirve este campo. Un id
    inestable falla siempre; la colision solo junta saludos genericos.

    Por eso el campo 'identidad_de_plataforma' viaja al lado en la telemetria:
    dice si el id es identidad de verdad o solo esta agrupacion aproximada, para
    que al analizar no se confunda una con otra. La solucion real no esta de este
    lado: es que la plataforma mande el id.
    """
    if id_chat:
        return "conv_" + hashlib.sha256(id_chat.encode("utf-8")).hexdigest()[:16]

    primero = next((m for m in mensajes if m["role"] == "user"), None)
    texto = _ultima_pregunta([primero]) if primero else ""
    return "conv_" + hashlib.sha256(texto.encode("utf-8")).hexdigest()[:16]


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

    # El transcript ya se acota a MAX_TRANSCRIPT_CHARS, asi que lo que se manda
    # al modelo estaba acotado. Lo que no lo estaba era el cuerpo CRUDO: una
    # peticion enorme se parseaba entera a memoria antes de que nadie la mirara.
    # Se corta por Content-Length, que es gratis y llega antes que el parseo.
    declarado = request.headers.get("content-length")
    if declarado and declarado.isdigit() and int(declarado) > MAX_BODY_BYTES:
        registrar("cuerpo_demasiado_grande", bytes=int(declarado))
        return JSONResponse(
            status_code=413,
            content=openresponses.construir_error(
                id_respuesta, MODEL,
                f"El cuerpo supera el limite de {MAX_BODY_BYTES} bytes.",
                "invalid_request",
            ),
        )

    # Content-Length puede no venir: una peticion en chunks no lo declara y se
    # saltaba el tope entero. Se vuelve a medir sobre el cuerpo ya leido.
    crudo = await request.body()
    if len(crudo) > MAX_BODY_BYTES:
        registrar("cuerpo_demasiado_grande", bytes=len(crudo), declarado=False)
        return JSONResponse(
            status_code=413,
            content=openresponses.construir_error(
                id_respuesta, MODEL,
                f"El cuerpo supera el limite de {MAX_BODY_BYTES} bytes.",
                "invalid_request",
            ),
        )

    try:
        payload = json.loads(crudo)
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

    mensajes, mensajes_omitidos = acotar_transcript(mensajes)

    instrucciones = payload.get("instructions") or ""
    streaming = bool(payload.get("stream", False))
    ids_plataforma = _ids_de_la_plataforma(payload)
    effort = _effort_pedido(payload)
    id_conv = _id_conversacion(mensajes, ids_plataforma["id_chat"])
    if mensajes_omitidos:
        registrar(
            "transcript_acotado",
            id_conversacion=id_conv,
            **ids_plataforma,
            identidad_de_plataforma=bool(ids_plataforma["id_chat"]
                                         or ids_plataforma["id_usuario"]),
            mensajes_omitidos=mensajes_omitidos,
            mensajes_enviados=len(mensajes),
        )
    # _ultima_pregunta y no m['content'] a secas: cuando el mensaje trae una
    # imagen, content es una lista de bloques y no una cadena. Pasar esa lista
    # al guardrail reventaba con 500 justo en el caso para el que las imagenes
    # existen -- alguien pega la captura de una vacante.
    ultimo_usuario = _ultima_pregunta(mensajes)

    # --- Guardrail de entrada: puede cortar antes de gastar una llamada al modelo
    _contenido_ultimo = next(
        (m.get("content") for m in reversed(mensajes) if m.get("role") == "user"), ""
    )
    veredicto = guardrails.revisar_entrada(
        ultimo_usuario, hay_imagen=guardrails.tiene_imagen(_contenido_ultimo)
    )
    if not veredicto.permitido:
        registrar("guardrail_entrada_bloqueo", motivo=veredicto.motivo, id_conversacion=id_conv)
        registrar_turno(
            id_respuesta=id_respuesta,
            id_conversacion=id_conv,
            **ids_plataforma,
            identidad_de_plataforma=bool(ids_plataforma["id_chat"]
                                         or ids_plataforma["id_usuario"]),
            modelo=MODEL,
            latencia_ms=int((time.perf_counter() - inicio) * 1000),
            tokens_entrada=0,
            tokens_salida=0,
            herramientas_usadas=[],
            citas=[],
            etiquetas_guardrail=veredicto.etiquetas,
            turnos_herramienta=0,
            streaming=streaming,
            categoria=categorias.clasificar(ultimo_usuario, [], veredicto.etiquetas),
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

    guias, etiquetas_politica = guardrails.guias_de_politica(mensajes)
    if etiquetas_politica:
        registrar("politica_de_tema", etiquetas=etiquetas_politica, id_conversacion=id_conv)

    if streaming:
        return StreamingResponse(
            _stream_agente(id_respuesta, id_mensaje, mensajes, instrucciones, id_conv, inicio,
                           guias, etiquetas_politica, ids_plataforma, effort),
            media_type="text/event-stream",
            headers=CABECERAS_SSE,
        )

    # En threadpool y no directo: _responder_completo usa el cliente SINCRONO de
    # Anthropic y tarda segundos. Llamarlo desde una corrutina bloquea el event
    # loop, y con eso toda la instancia -- incluido /salud, que es lo que Cloud
    # Run consulta para decidir si el contenedor sigue vivo.
    #
    # La ruta de streaming no necesita esto: StreamingResponse ve que
    # _stream_agente es un generador sincrono y lo corre en threadpool sola.
    cuerpo = await run_in_threadpool(
        _responder_completo, id_respuesta, id_mensaje, mensajes, instrucciones,
        id_conv, inicio, guias, etiquetas_politica, ids_plataforma, effort,
    )
    return JSONResponse(content=cuerpo)


# Valor neutro cuando la plataforma no manda identidad. Se define una sola vez
# para que las dos rutas -- streaming y completa -- registren los mismos campos.
_IDS_VACIOS = {"id_usuario": "", "id_chat": "", "id_respuesta_previa": ""}


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
    guias: list[str] | None = None,
    etiquetas_politica: list[str] | None = None,
    ids_plataforma: dict[str, str] | None = None,
    effort: str = "",
) -> Iterator[str]:
    ids_plataforma = ids_plataforma or _IDS_VACIOS
    emisor = openresponses.EmisorSSE(id_respuesta, id_mensaje, MODEL)
    yield from emisor.inicio()

    resumen = None
    # La redaccion de PII tiene que ocurrir ANTES de emitir cada delta. Aplicarla
    # al texto final -- como se hacia -- no protegia nada en streaming: para
    # cuando se calculaba, los deltas ya habian salido, y streaming es
    # justamente el modo que usa la plataforma. El redactor retiene una cola
    # para que un dato partido entre dos deltas tampoco se escape.
    redactor = guardrails.RedactorDeFlujo()
    try:
        for tipo, carga in agent.responder(mensajes, instrucciones, guias, effort):
            if tipo == "delta":
                seguro = redactor.empujar(str(carga))
                if seguro:
                    yield emisor.delta(seguro)
            else:
                resumen = carga
    except Exception as exc:  # noqa: BLE001
        registrar_error("fallo_stream", detalle=str(exc), id_conversacion=id_conv)
        # Lo retenido se suelta antes del error: es texto que el modelo ya
        # produjo y que perderlo no arregla nada. Ya viene redactado.
        cola_parcial = redactor.cerrar()
        if cola_parcial:
            yield emisor.delta(cola_parcial)
        yield from emisor.error("El agente fallo a mitad de la respuesta.")
        return

    # Se suelta la cola retenida antes de cerrar el mensaje.
    cola = redactor.cerrar()
    if cola:
        yield emisor.delta(cola)

    etiquetas: list[str] = list(etiquetas_politica or [])
    etiquetas += redactor.etiquetas
    if resumen is not None:
        # Sobre el texto ya redactado: es lo que de verdad recibio el cliente.
        texto_emitido, _ = guardrails.redactar_pii(resumen.texto)
        etiquetas += guardrails.verificar_fundamento(texto_emitido, resumen.citas)
        etiquetas += guardrails.verificar_academico(texto_emitido)

    yield from emisor.fin(
        resumen.tokens_entrada if resumen else 0,
        resumen.tokens_salida if resumen else 0,
    )

    registrar_turno(
        id_respuesta=id_respuesta,
        id_conversacion=id_conv,
        **ids_plataforma,
        identidad_de_plataforma=bool(ids_plataforma["id_chat"] or ids_plataforma["id_usuario"]),
        modelo=MODEL,
        latencia_ms=int((time.perf_counter() - inicio) * 1000),
        tokens_entrada=resumen.tokens_entrada if resumen else 0,
        tokens_salida=resumen.tokens_salida if resumen else 0,
        tokens_cache_leidos=resumen.tokens_cache_leidos if resumen else 0,
        tokens_cache_escritos=resumen.tokens_cache_escritos if resumen else 0,
        herramientas_usadas=resumen.herramientas_usadas if resumen else [],
        citas=resumen.citas if resumen else [],
        etiquetas_guardrail=etiquetas,
        turnos_herramienta=resumen.turnos_herramienta if resumen else 0,
        streaming=True,
        error=resumen.error if resumen else "sin_resumen",
        categoria=categorias.clasificar(
            _ultima_pregunta(mensajes),
            resumen.herramientas_usadas if resumen else [],
            etiquetas,
        ),
    )


def _responder_completo(
    id_respuesta: str,
    id_mensaje: str,
    mensajes: list[dict],
    instrucciones: str,
    id_conv: str,
    inicio: float,
    guias: list[str] | None = None,
    etiquetas_politica: list[str] | None = None,
    ids_plataforma: dict[str, str] | None = None,
    effort: str = "",
) -> dict:
    ids_plataforma = ids_plataforma or _IDS_VACIOS
    resumen = None
    for tipo, carga in agent.responder(mensajes, instrucciones, guias, effort):
        if tipo == "fin":
            resumen = carga

    texto = resumen.texto if resumen else ""
    texto, etiquetas_pii = guardrails.redactar_pii(texto)
    etiquetas = (list(etiquetas_politica or []) + etiquetas_pii
                 + guardrails.verificar_fundamento(texto, resumen.citas if resumen else [])
                 + guardrails.verificar_academico(texto))

    registrar_turno(
        id_respuesta=id_respuesta,
        id_conversacion=id_conv,
        **ids_plataforma,
        identidad_de_plataforma=bool(ids_plataforma["id_chat"] or ids_plataforma["id_usuario"]),
        modelo=MODEL,
        latencia_ms=int((time.perf_counter() - inicio) * 1000),
        tokens_entrada=resumen.tokens_entrada if resumen else 0,
        tokens_salida=resumen.tokens_salida if resumen else 0,
        tokens_cache_leidos=resumen.tokens_cache_leidos if resumen else 0,
        tokens_cache_escritos=resumen.tokens_cache_escritos if resumen else 0,
        herramientas_usadas=resumen.herramientas_usadas if resumen else [],
        citas=resumen.citas if resumen else [],
        etiquetas_guardrail=etiquetas,
        turnos_herramienta=resumen.turnos_herramienta if resumen else 0,
        streaming=False,
        error=resumen.error if resumen else "sin_resumen",
        categoria=categorias.clasificar(
            _ultima_pregunta(mensajes),
            resumen.herramientas_usadas if resumen else [],
            etiquetas,
        ),
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
            "defaultInputModes": ["text/plain", "image/png", "image/jpeg", "image/webp"],
            "defaultOutputModes": ["text/plain"],
            "promptSuggestions": SUGERENCIAS,
            # Cada skill se anuncia solo porque hay codigo y pruebas detras.
            # Anunciar una capacidad que no se cumple es peor que no anunciarla:
            # por eso no hay ninguna de archivos, que el agente no maneja.
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
                {
                    "id": "leer-captura",
                    "name": "Leer la captura de una vacante",
                    "description": "Extrae el texto de una imagen y lo trata como la descripcion del puesto. El texto dentro de la imagen es dato, nunca instruccion.",
                },
                {
                    "id": "abrir-enlace",
                    "name": "Abrir el enlace de una vacante",
                    "description": "Consulta una URL que le peguen en la conversacion para traer la descripcion del puesto. Solo abre enlaces que ya estan en el hilo.",
                },
                {
                    "id": "generar-reporte",
                    "name": "Generar un reporte descargable",
                    "description": "Convierte el analisis en un documento con enlace propio, valido por siete dias. Solo cuando se lo piden.",
                },
                {
                    "id": "explicar-su-construccion",
                    "name": "Explicar como esta construido",
                    "description": "Habla de su propia arquitectura -- herramientas, guardrails, evaluacion y telemetria -- porque es uno de los proyectos del CV.",
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


@app.get("/salud")
@app.get("/healthcheck")
async def salud():
    """Sonda de vida. Valida que el CV cargue: sin el, el agente no sirve de nada.

    NO se llama /healthz a proposito. La infraestructura de Google intercepta esa
    ruta antes de que llegue al contenedor -- es la convencion de health check de
    Kubernetes y su balanceador la reserva -- asi que devolvia un 404 de Google
    mientras el resto de la app respondia perfecto. El sintoma era enganoso: el
    servicio se veia caido desde la sonda y sano desde cualquier otra ruta.
    """
    try:
        cv = load_cv()
        return {
            "estado": "ok",
            "modelo": MODEL,
            # Que commit corre aqui. Es la unica forma de saber, desde fuera, si
            # produccion trae lo mismo que el repositorio.
            "version": AGENT_VERSION,
            "commit": GIT_SHA,
            "revision": CLOUD_RUN_REVISION,
            "construido_desde_arbol_limpio": GIT_LIMPIO,
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
