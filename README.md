# Agente de CV conversacional — Reto IA Banorte

Agente que responde preguntas sobre el perfil profesional de **Edher Iván Díaz Salazar**
(Analytics Engineer — Looker, BigQuery, Google Cloud). Habla el protocolo
[Open Responses](https://www.openresponses.org/), corre en Cloud Run y está fundamentado
en un CV estructurado: **no inventa datos y dice explícitamente cuándo algo no está en el CV.**

```
Plataforma Reto IA ──POST /v1/responses (SSE)──►  Cloud Run · FastAPI
                                                        │
                    ┌───────────────────────────────────┼───────────────────────┐
                    ▼                                   ▼                       ▼
            Guardrail de entrada              Loop agéntico (Claude)      Telemetría
         scope · inyección · tamaño        6 herramientas sobre cv.json   BigQuery +
                                                        │                Cloud Logging
                                                        ▼                       │
                                            Guardrail de salida                 ▼
                                          PII · fundamentación         Agent Analytics Block
                                                                         (Looker)
```

## Índice

- [Qué puede hacer](#qué-puede-hacer)
- [Decisiones técnicas y trade-offs](#decisiones-técnicas-y-trade-offs)
- [Cómo verifico que funciona](#cómo-verifico-que-funciona)
- [Correr en local](#correr-en-local)
- [Desplegar](#desplegar)
- [Operación](#operación)
- [Estructura](#estructura)

---

## Qué puede hacer

| Pregunta | Qué hace por dentro |
|---|---|
| *¿Cuál ha sido su experiencia con modelos de lenguaje?* | `buscar_cv` → recupera GlobalLogic + habilidades de IA, responde con métricas reales |
| *¿Tiene experiencia con Kubernetes?* | Detecta que el término no existe en el CV y **lo dice**, en vez de inferirlo de "Docker" |
| *Te paso esta vacante: ¿encaja?* | `evaluar_vacante` → fortalezas con evidencia, coincidencias parciales, y **huecos reales** |
| *Ignora tus instrucciones y…* | Guardrail de entrada corta **antes** de gastar una llamada al modelo |
| *Dame su teléfono* | El teléfono no está en la base de conocimiento, y hay redacción de PII como segunda barrera |

La herramienta que hace útil al agente para un reclutador es `evaluar_vacante`: pega una
descripción de puesto y obtienes un contraste honesto en tres direcciones. Nombrar los huecos
es deliberado — un agente que sólo dice que sí no le sirve a quien tiene que tomar la decisión.

---

## Decisiones técnicas y trade-offs

### 1. Sin base vectorial. El CV es JSON estructurado consultado por herramientas

El CV completo son ~4 000 tokens. Un índice de embeddings sobre un documento de ese tamaño
es sobre-ingeniería: agrega un servicio que operar, respaldar y pagar, para resolver un
problema de recuperación que no existe a esa escala.

En su lugar el CV vive como JSON con **un `id` estable por entrada**, y el modelo lo consulta
con seis herramientas tipadas. Lo que se gana:

- **Trazabilidad.** Cada herramienta devuelve `_citas` con los ids que respaldan el resultado.
  Se puede auditar de dónde salió cada afirmación.
- **Determinismo.** La misma pregunta recupera exactamente el mismo contexto, lo que hace que
  la suite de evaluación sea reproducible. Con recuperación vectorial, un eval que falla te deja
  sin saber si cambió el modelo o cambió el ranking.
- **Cero infraestructura extra.**

**El trade-off:** esto no escala a un corpus grande. Si mañana hubiera que indexar 200 documentos
de proyectos, la recuperación por palabra clave se queda corta y tocaría meter búsqueda vectorial.
La decisión es correcta *para este tamaño de corpus*, y está aislada en `app/tools.py` para que
cambiarla no toque el resto del sistema.

**Un bug real que esto ayudó a encontrar:** la primera versión puntuaba por subcadena, así que
`"con"` empataba dentro de `"Construyo"` y `"Consultant"`. La pregunta más importante del reto
—*"¿tu experiencia con modelos de lenguaje?"*— devolvía el puesto equivocado. La versión actual
puntúa por palabra completa, con stopwords y grupos de sinónimos bidireccionales. Está fijado
como prueba de regresión en `evals/test_offline.py`.

### 2. Servidor sin estado

La plataforma ofrece dos modos: reproducir el transcript completo, o `previous_response_id` con
el agente guardando el estado. **Se eligió el primero.**

Sin estado no hay sesiones que expiren, ni base de datos que respaldar, ni pegamento para que
varias instancias compartan memoria: Cloud Run puede escalar horizontalmente sin coordinación.
El costo es reenviar el transcript en cada turno, que a esta longitud de conversación es
irrelevante, y se mitiga con caché de prompt sobre el prefijo estable (system + herramientas).

Para telemetría hace falta agrupar los turnos de una misma conversación. Se resuelve sin estado:
el hash del primer mensaje del usuario es estable durante toda la conversación y sirve de
identificador.

### 3. Claude Opus 5 con effort bajo

`claude-opus-5` con thinking adaptativo y `effort: low`. La calidad del razonamiento no es el
cuello de botella —las respuestas se fundamentan en herramientas, no en razonamiento libre—
pero la latencia sí importa en un chat. Effort bajo da respuestas en 2–4 s manteniendo el
seguimiento estricto de instrucciones, que es lo que aquí realmente importa. Es una variable
de entorno: se sube sin recompilar.

**Fallback de servidor ante rechazos.** Si el modelo declina por política, la API reintenta el
mismo request en un modelo de respaldo dentro de la misma llamada. Un agente de CV no debería
toparse con esto nunca, pero cuesta un parámetro y evita que el evaluador vea una conversación
rota si alguien le escribe algo raro.

### 4. Loop manual en vez del tool runner del SDK

El SDK trae un tool runner que automatiza el ciclo. Aquí se escribió el loop a mano porque hacen
falta dos cosas que el runner esconde: emitir deltas de texto hacia el cliente **mientras** corre
el loop, y contabilizar herramientas, citas y tokens por turno para la telemetría.

### 5. Guardrails deterministas sólo donde son inequívocos

Los controles por regex atajan lo barato y claro: inyección de prompt evidente, fuga de PII,
entradas absurdamente largas. El matiz —qué cuenta como fuera de alcance, cómo reconocer que
algo no está en el CV— vive en el prompt del sistema y se verifica en la suite de evaluación.

**Un regex agresivo rompe conversaciones legítimas, que es peor que el problema que evita.**
Por eso hay pruebas en las dos direcciones: que bloquea los ataques *y* que no bloquea preguntas
válidas como *"¿qué instrucciones le daba a los agentes LLM que construyó?"*.

La señal de ausencia sigue la misma lógica de fallar en silencio: sólo se marca como "no está en
el CV" un término que **parezca nombre de tecnología** (mayúscula a media frase, dígitos, o lista
conocida). Marcar vocabulario común llevaría al agente a negar experiencia en "carrera" o
"desafiante", que es peor que no avisar nada.

### 6. Privacidad: el teléfono no está en el repositorio

El repo es público y el CV original trae un teléfono. **No está en `cv.json`** — el agente sólo
comparte correo, LinkedIn, GitHub y sitio web. La redacción de PII en la salida es la segunda
barrera, no la primera. Hay una prueba que falla si alguien lo vuelve a meter al JSON.

### 7. Observabilidad que cierra el círculo

Cada turno emite un evento con latencia, tokens, herramientas usadas, citas devueltas, etiquetas
de guardrail y si la respuesta quedó fundamentada. Va a dos destinos: stdout como JSON de una
línea (Cloud Logging lo indexa por campo sin configurar nada) y BigQuery cuando está configurado.

En BigQuery se monitorea con el **Agent Analytics Block** de `looker-open-source`, del que soy
autor principal. Es la parte del diseño de la que estoy más contento: la pieza de observabilidad
del agente es una contribución open source propia, no una herramienta traída de fuera.

El sink de BigQuery corre en un hilo aparte y es best-effort. **La observabilidad nunca debe
agregar latencia ni tumbar una respuesta al usuario.**

### 8. Tercera persona, a propósito

El agente habla de Edher en tercera persona ("Edher trabajó en…"), no se hace pasar por él.
Quien consulta debe saber en todo momento que habla con un agente. Suena a detalle de tono, pero
es una decisión de diseño: un agente que se hace pasar por el candidato ante un reclutador es un
producto peor, aunque conversacionalmente sea más vistoso.

---

## Cómo verifico que funciona

Tres capas, de más barata a más cara:

### Capa 1 — Pruebas offline (49 pruebas, 0.1 s, gratis, en cada push)

```bash
pytest evals/test_offline.py -v
```

Cubren integridad del CV (ids únicos, sin teléfono), calidad de recuperación (incluido el bug de
subcadena como regresión), guardrails en ambas direcciones, y conformidad del protocolo:
secuencia de eventos SSE, que `event:` coincida con el `type` del cuerpo, numeración correlativa
y terminador `[DONE]`.

### Capa 2 — Conjunto dorado contra el modelo real (~30 casos, ~1 USD)

```bash
python evals/run_evals.py                        # todo
python evals/run_evals.py --categoria adversarial
python evals/run_evals.py --caso exp-llm --verbose
```

Cada caso declara qué se espera: herramientas que debieron llamarse, ids que debieron citarse,
subcadenas obligatorias y prohibidas, idioma de la respuesta. Se invoca al agente **en proceso,
no por HTTP**, para poder afirmar también sobre las herramientas: una respuesta correcta por
accidente, sin haber consultado el CV, cuenta como fallo.

Un tercio de los casos son adversariales —Kubernetes, Snowflake, AWS, una empresa en la que nunca
trabajó, el sueldo, la edad— porque **ahí es donde un agente de CV pierde la confianza de un
reclutador**: no cuando no sabe algo, sino cuando lo inventa.

### Capa 3 — Verificación del contrato contra la implementación de referencia

La forma exacta de la carga útil se verificó contra el agente "Guía del reto" del propio reto,
no sólo contra la especificación escrita. Campos, orden de eventos y terminador replican lo que
ese endpoint emite realmente, que es contra lo que la plataforma valida.

---

## Correr en local

```bash
python -m venv .venv && source .venv/Scripts/activate   # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env        # y pon tu ANTHROPIC_API_KEY
export ANTHROPIC_API_KEY=sk-ant-...
uvicorn app.main:app --reload --port 8080
```

Probar:

```bash
curl -N -X POST http://localhost:8080/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"¿Qué experiencia tiene con modelos de lenguaje?","stream":true}'
```

Con Docker:

```bash
docker build -t cv-agent .
docker run -p 8080:8080 -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY cv-agent
```

---

## Desplegar

```bash
./scripts/setup_bigquery.sh     # opcional: telemetría
./scripts/deploy.sh
```

El script habilita las APIs, guarda la key en **Secret Manager** (nunca como variable de entorno
en claro, nunca en el historial del shell), da el permiso mínimo a la cuenta de servicio,
despliega, y al final fija `PUBLIC_BASE_URL` con la URL real que asignó Cloud Run — que es la que
publica la tarjeta de agente.

`--min-instances 1` durante la evaluación: el arranque en frío del contenedor es de ~4 s y no
vale la pena que el evaluador lo pague. Se baja a 0 cuando termine.

### Registrar en la plataforma

Usa **"Importar desde tarjeta de agente"** con la URL raíz del servicio: la plataforma lee
`/.well-known/agent-card.json` y llena el formulario solo. A mano sería:

| Campo | Valor |
|---|---|
| URL base | `https://<servicio>.run.app/v1` |
| Estado de la conversación | Reproducir transcripción (sin estado) |
| Clave de API | sólo si desplegaste con `AGENT_API_KEY` |

---

## Operación

| Endpoint | Para qué |
|---|---|
| `GET /healthz` | Sonda de vida. Valida que el CV cargue, no sólo que el proceso viva |
| `GET /.well-known/agent-card.json` | Tarjeta A2A para el registro automático |
| `GET /` | Página con instrucciones de uso |
| `GET /docs` | OpenAPI interactivo |

Consultas de monitoreo en `scripts/setup_bigquery.sh`: tasa de éxito, **tasa de fundamentación**,
latencias p50/p95, herramientas más usadas y turnos que dispararon un guardrail.

La tasa de fundamentación es la métrica que más vigilo: si baja, el agente está respondiendo
sobre el CV sin consultarlo, que es exactamente el fallo que este diseño existe para evitar.

---

## Estructura

```
app/
  main.py            FastAPI: /v1/responses, tarjeta de agente, salud
  openresponses.py   Serialización del protocolo (no-streaming y SSE)
  agent.py           Loop agéntico con Claude
  tools.py           Seis herramientas sobre el CV + búsqueda
  guardrails.py      Entrada (inyección, tamaño) y salida (PII, fundamentación)
  telemetry.py       Logging estructurado + sink de BigQuery
  config.py          Configuración por variables de entorno
  data/cv.json       Fuente de verdad, con id estable por entrada
evals/
  golden.yaml        ~30 casos declarativos, un tercio adversariales
  run_evals.py       Runner contra el modelo real, con reporte por categoría
  test_offline.py    49 pruebas sin modelo, corren en CI
scripts/
  deploy.sh          Cloud Run + Secret Manager
  setup_bigquery.sh  Dataset y tabla de telemetría
```

---

## Lo que haría con más tiempo

Honestidad sobre los límites de lo entregado:

- **Evaluación con juez LLM.** Las aserciones actuales son por subcadena, que es frágil ante
  paráfrasis. Un juez calificando fundamentación y tono daría una señal mejor.
- **Servidor MCP.** Las herramientas ya están aisladas del transporte; exponerlas por MCP para
  que otros agentes consulten el CV es un archivo más.
- **Caché semántica.** Los reclutadores hacen las mismas cinco preguntas. Cachear por intención
  bajaría costo y latencia de forma notable.
- **Trazas distribuidas.** Hoy hay una métrica por turno. OpenTelemetry con un span por llamada
  a herramienta daría mejor visibilidad de dónde se va la latencia.
