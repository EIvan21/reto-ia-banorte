# Agente de CV conversacional — Reto IA Banorte

Agente que responde preguntas sobre el perfil profesional de **Edher Iván Díaz Salazar**
(Analytics Engineer e ingeniero de agentes de IA). Habla el protocolo
[Open Responses](https://www.openresponses.org/), corre en Cloud Run, y está fundamentado en un
CV estructurado: no inventa datos, y dice explícitamente cuándo algo no está en el CV.

**En vivo:** https://cv-agent-npnpuwxi2q-uc.a.run.app

```bash
curl https://cv-agent-npnpuwxi2q-uc.a.run.app/.well-known/agent-card.json
```

---

## Qué puedes preguntarle

| Pregunta | Qué hace por dentro |
|---|---|
| *¿Cuál ha sido su experiencia con modelos de lenguaje?* | `buscar_cv` → recupera GlobalLogic + habilidades de IA, responde con métricas reales |
| *¿Tiene experiencia con Kubernetes?* | Detecta que el término no existe en el CV y lo dice, en vez de inferirlo de "Docker" |
| *Te paso esta vacante: ¿encaja?* | `evaluar_vacante` → fortalezas con evidencia, coincidencias parciales, y huecos reales |
| *Ignora tus instrucciones y…* | El guardrail de entrada corta antes de gastar una llamada al modelo |
| *Dame su teléfono* | El teléfono no está en la base de conocimiento, y hay redacción de PII como segunda barrera |

La herramienta que lo hace útil para un reclutador es `evaluar_vacante`: pega una descripción de
puesto y obtienes un contraste en tres direcciones. Nombrar los huecos es deliberado — un agente
que sólo dice que sí no le sirve a quien tiene que decidir.

---

## Cómo está hecho

```
Plataforma Reto IA ──POST /v1/responses (SSE)──►  Cloud Run · FastAPI
Cualquier agente   ──POST /mcp (streamable)────►        │
                                                        │
                    ┌───────────────────────────────────┼───────────────────────┐
                    ▼                                   ▼                       ▼
            Guardrail de entrada              Loop agéntico (Claude)      Telemetría
         alcance · inyección · tamaño      7 herramientas sobre cv.json   BigQuery +
                                                        │                Cloud Logging
                                                        ▼                       │
                                            Guardrail de salida                 ▼
                                     PII · fundamentación · formación   Agent Analytics Block
                                                                              (Looker)
```

**Stack:** FastAPI · Claude Opus 5 · Cloud Run · MCP · Open Responses · Secret Manager ·
Cloud Storage · BigQuery + Agent Analytics Block (Looker)

**Endpoints:** `POST /v1/responses` (Bearer) · `POST /mcp` ·
`GET /.well-known/agent-card.json` · `GET /salud`

---

## Decisiones clave

Cada una está desarrollada en **[DECISIONES.md](DECISIONES.md)**, con el porqué y las
alternativas que se descartaron.

- **Sin base vectorial.** 30 entradas indexables; recuperación léxica con IDF. La brecha entre
  preguntas que comparten vocabulario con el CV y preguntas que no es de 0.01 en MRR — que es
  justo donde los vectores deberían ganar. `evals/medir_corpus.py` mide cuándo eso cambiaría.
- **Servidor sin estado.** La plataforma reenvía el transcript; Cloud Run escala sin coordinación.
  Dos puntos de caché hacen que el 77–95% de la entrada se sirva desde caché a partir del segundo
  turno, sin resumir ni perder un solo hecho.
- **Guardrails deterministas sólo donde son inequívocos.** El matiz vive en el prompt y se
  verifica en la suite. Un regex agresivo rompe conversaciones legítimas, que es peor que el
  problema que evita.
- **El mismo CV por dos protocolos.** Open Responses para la plataforma, MCP para cualquier otro
  agente. Un solo despliegue.
- **Tercera persona, siempre.** El agente representa el CV; nunca se hace pasar por Edher.

---

## Cómo se verifica

- **263 pruebas offline** — recuperación, guardrails, protocolo, imágenes, streaming. Corren en
  CI en cada push, en ~2 s y sin API key.
- **45/45 en el conjunto dorado** — casos contra el modelo real, con aserciones sobre qué
  herramienta se usó y qué se citó. Se corre a mano antes de desplegar.
- **Contrato validado** contra la implementación de referencia de Open Responses.

```bash
pytest evals/test_offline.py -q          # gratis
python evals/run_evals.py                # llama al modelo
python evals/medir_corpus.py             # ¿sigue bastando la búsqueda léxica?
```

---

## Correr en local

```bash
python -m venv .venv && source .venv/Scripts/activate
pip install -r requirements-dev.txt
cp .env.example .env        # y pon tu ANTHROPIC_API_KEY
uvicorn app.main:app --reload --port 8080
```

```bash
curl -N -X POST http://localhost:8080/v1/responses \
  -H "Content-Type: application/json" \
  -d '{"input":"¿Qué experiencia tiene con modelos de lenguaje?","stream":true}'
```

Para desplegar, registrar en la plataforma y operar: **[docs/OPERACION.md](docs/OPERACION.md)**.

---

## Siguiente iteración

Lo que está identificado y no entró, con el motivo:

- **Fundamentación más estricta.** Hoy una respuesta cuenta como fundamentada si el agente citó
  al menos una entrada. Un modelo puede citar y luego inventar alrededor. La versión fuerte:
  verificar que las cifras y nombres propios de la respuesta aparezcan en las entradas citadas.
- **Aserciones sobre cifras en el conjunto dorado.** Misma idea, del lado de la evaluación: que
  todo número de una respuesta exista en lo que se citó.
- **Verificar el fallback ante rechazos.** Se usa `betas` + `fallbacks` del SDK. Funciona, pero
  no está contrastado contra la documentación oficial, así que el README no afirma más que eso.
- **Juez de fidelidad** (Ragas o un juez propio) sobre el conjunto dorado, usando como contexto
  las entradas citadas. Es el paso natural después del punto anterior.
- **Conjunto dorado en CI**, programado y con la API key como secreto. Hoy sólo corren las
  pruebas offline en cada push, a propósito: el conjunto dorado cuesta dinero y la decisión de
  "esto ya está listo" se toma mirando los resultados.
- **Rate limiting.** Con `max-instances` el gasto está acotado por diseño. Un limitador en
  memoria no sirve con varias instancias; lo correcto es Cloud Armor, que es infraestructura.
- **Tabla OWASP LLM Top 10** mapeando cada control existente a su categoría.
