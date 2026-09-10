# Operación

Todo lo que hace falta para desplegar, registrar y operar el agente. Vive aparte del
README a propósito: quien abre el repositorio quiere ver qué es y probarlo, no leer
cómo se rotan los secretos.

Para las decisiones de arquitectura y por qué están tomadas así, ver [DECISIONES.md](../DECISIONES.md).

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
| `POST /mcp` | Servidor MCP con las mismas herramientas, para cualquier otro agente |
| `GET /salud` | Sonda de vida. Valida que el CV cargue, no sólo que el proceso viva |
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
  mcp_server.py      Las mismas herramientas por MCP (HTTP montado + stdio)
  tools.py           Siete herramientas sobre el CV + búsqueda
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

