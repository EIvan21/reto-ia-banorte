# Guía de decisiones técnicas

Este documento existe para una cosa: que puedas responder **"¿por qué hiciste esto así?"**
sobre cualquier pieza del sistema, y que además sepas qué alternativas había y cuándo habrías
elegido otra.

Cada decisión sigue el mismo formato: **qué es → por qué esto → qué más había → cuándo elegiría
la otra**. Esa última parte es la que separa a alguien que copió un tutorial de alguien que tomó
una decisión.

---

## Índice

1. [El problema en una frase](#1-el-problema-en-una-frase)
2. [Cómo funciona, de punta a punta](#2-cómo-funciona-de-punta-a-punta)
3. [El stack, herramienta por herramienta](#3-el-stack-herramienta-por-herramienta)
4. [La estructura de archivos y por qué](#4-la-estructura-de-archivos-y-por-qué)
5. [Las decisiones de arquitectura](#5-las-decisiones-de-arquitectura)
6. [Cómo se verifica que funciona](#6-cómo-se-verifica-que-funciona)
7. [Los bugs que encontramos](#7-los-bugs-que-encontramos-y-qué-enseñan)
8. [Preguntas que te van a hacer](#8-preguntas-que-te-van-a-hacer)

---

## 1. El problema en una frase

Construir un agente que converse sobre tu perfil profesional, **desplegado y operable**,
compatible con el protocolo Open Responses para que la plataforma del reto lo pueda consumir.

El reto dice explícitamente que no busca "una interfaz bonita" sino criterio técnico. Así que
todo lo que sigue está orientado a una cosa: **que cada decisión tenga una razón defendible.**

---

## 2. Cómo funciona, de punta a punta

Cuando alguien escribe *"¿qué experiencia tiene con modelos de lenguaje?"* en la plataforma:

```
1. La plataforma manda POST /v1/responses con el transcript completo
        │
2. Se valida el Bearer token
        │
3. Se parsea el input (acepta string o lista de mensajes)
        │
4. GUARDRAIL DE ENTRADA  ── ¿inyección de prompt? ── corta ANTES de gastar la llamada
        │
5. POLÍTICAS DE TEMA     ── ¿salario? ¿vida privada? ── inyecta guía de manejo
        │
6. LOOP AGÉNTICO
        ├─ Claude recibe: system + herramientas + transcript
        ├─ Claude decide llamar buscar_cv("modelos de lenguaje")
        ├─ Se ejecuta la herramienta sobre cv.json
        ├─ Devuelve entradas + _citas: ["hab-ia", "exp-globallogic"]
        └─ Claude compone la respuesta con esos datos
        │
7. Los deltas de texto salen como eventos SSE mientras se generan
        │
8. GUARDRAIL DE SALIDA   ── redacción de PII, verificación de fundamento
        │
9. TELEMETRÍA            ── latencia, tokens, herramientas, citas → stdout + BigQuery
```

**El punto clave del diseño:** el paso 6 nunca inventa. Claude solo puede afirmar lo que las
herramientas le devolvieron, y cada resultado trae los `id` que lo respaldan.

---

## 3. El stack, herramienta por herramienta

### Python 3.11

**Por qué:** es el lenguaje con el mejor ecosistema para trabajo con LLMs — el SDK de Anthropic,
el de MCP y las librerías de datos son de primera clase ahí. Además es tu lenguaje del día a día.

**Alternativas:** TypeScript/Node tiene SDKs igual de buenos y sería mejor si el equipo fuera de
frontend o si quisieras desplegar en edge (Vercel, Cloudflare Workers). Go daría binarios más
pequeños y arranque en frío más rápido, que importa en serverless, pero su ecosistema de IA es
más delgado.

**Cuándo elegiría otra:** TypeScript si el agente viviera dentro de una app web existente. Go si
el arranque en frío fuera crítico y las herramientas fueran simples.

### FastAPI

**Qué es:** framework web asíncrono para Python.

**Por qué:** tres razones concretas. Soporta streaming SSE nativo con `StreamingResponse`, que
este protocolo exige. Genera documentación OpenAPI sola (`/docs`). Y permite **montar sub-apps**,
que es exactamente lo que hizo posible servir MCP en `/mcp` dentro del mismo proceso.

**Alternativas:** Flask es más simple pero síncrono por defecto, y el streaming se vuelve
incómodo. Starlette es lo que FastAPI usa por debajo — más ligero, pero pierdes la validación
automática. Django es demasiado para un servicio de dos endpoints.

**Cuándo elegiría otra:** Starlette solo si quisiera cero dependencias extra. Django si esto
fuera parte de una aplicación con base de datos, usuarios y panel de administración.

### Claude (Opus 5) vía el SDK de Anthropic

**Por qué:** el trabajo aquí no es razonamiento libre, es **seguir instrucciones con precisión**
— usar herramientas, no inventar, respetar políticas. Ahí es donde el modelo tiene que ser
bueno. Se corre con `effort: low` porque en un chat la latencia importa más que la profundidad
de razonamiento, y las respuestas ya vienen fundamentadas en datos.

**Alternativas:** GPT sería la ruta con menos fricción porque su formato nativo es casi idéntico
a Open Responses (la capa de traducción sería mínima). Gemini tendría sentido por afinidad con
Google Cloud y tiene free tier. Un modelo abierto (Llama, Qwen) evitaría el costo por token pero
te obliga a operar la inferencia.

**Cuándo elegiría otra:** GPT si el tiempo fuera muy corto y quisiera saltarme la capa de
traducción. Gemini si el costo fuera cero-o-nada. Un modelo abierto si hubiera requisito de que
los datos no salgan de tu infraestructura — que en un banco es una conversación real.

### Protocolo Open Responses

**Por qué:** no fue una elección, es el requisito del reto. Pero sí hubo una decisión dentro:
**verificamos el formato contra el agente de referencia del propio reto**, no solo contra la
especificación escrita. Le mandamos peticiones a su endpoint y replicamos campo por campo lo que
devuelve. La especificación describe el contrato; la implementación de referencia es contra lo
que realmente te validan.

### MCP (Model Context Protocol)

**Qué es:** protocolo abierto para que un agente exponga herramientas a otros agentes.

**Por qué:** el brief lo nombra entre lo valorado, y tú dijiste que quieres trabajar en creación
de servidores MCP. Demostrarlo con código pesa más que escribirlo en el CV. Y costó **un archivo**
— porque las herramientas ya vivían separadas del transporte.

**Alternativas:** no hacerlo. O exponer una API REST propia, que funciona pero no es un estándar
que otros agentes hablen.

### Docker + Cloud Run

**Por qué Cloud Run:** eres Google Cloud Certified Associate Cloud Engineer, así que es tu stack
certificado y lo puedes defender a fondo. Técnicamente da contenedores con escalado a cero,
HTTPS gratis y despliegue desde código fuente. Se puso `--min-instances 1` durante la evaluación
porque el arranque en frío son ~4 s y no vale la pena que el evaluador los pague.

**Alternativas:**

| Opción | A favor | En contra |
|---|---|---|
| **Cloud Functions** | Aún más simple | Mal ajuste para SSE de larga duración |
| **GKE** | Control total | Operar un clúster para un servicio es absurdo |
| **App Engine** | Muy gestionado | Menos control sobre el runtime |
| **AWS Lambda + API GW** | Equivalente en AWS | Fuera de tu stack; el streaming es más incómodo |
| **Hugging Face Spaces** | Gratis, cero fricción | Menos historia de infraestructura que contar |
| **Azure Container Apps** | Donde corre el agente del reto | No es tu stack certificado |

**Cuándo elegiría otra:** Cloud Functions si no hubiera streaming. GKE si esto fuera parte de una
plataforma con diez servicios. Spaces si no hubiera presupuesto ni cuenta cloud.

### Secret Manager

**Por qué:** la API key nunca aparece como variable de entorno en claro, ni en el historial del
shell, ni en la configuración del servicio, ni en los logs. Cloud Run la monta en tiempo de
ejecución.

**Alternativas:** variables de entorno directas (más simple, pero la clave queda visible en la
consola de Cloud Run para cualquiera con acceso de lectura), o un archivo cifrado en el
repositorio con SOPS.

### BigQuery para telemetría

**Por qué:** es tu terreno, y cierra un círculo — la telemetría del agente se monitorea con el
**Agent Analytics Block** que tú escribiste. La pieza de observabilidad es una contribución open
source propia, no una herramienta traída de fuera.

**Alternativas:** solo Cloud Logging (gratis, suficiente para depurar, pero no analizable con
SQL). Prometheus + Grafana (estándar de la industria, pero es infraestructura extra que operar).
Datadog o New Relic (excelentes y caros).

**Detalle de implementación:** el envío a BigQuery corre en un hilo aparte y es *best-effort*.
**La observabilidad nunca debe agregar latencia ni tumbar una respuesta al usuario.** Si BigQuery
falla, se anota en stdout y la conversación sigue.

### pytest + un conjunto dorado en YAML

**Por qué dos capas separadas:** las pruebas offline no llaman al modelo, así que corren en 1
segundo, son gratis y van en CI en cada push. El conjunto dorado sí llama al modelo, cuesta ~1
USD por corrida, y se corre a mano antes de desplegar. Mezclarlas haría que CI costara dinero.

---

## 4. La estructura de archivos y por qué

```
app/
  main.py            FastAPI: endpoints, autenticación, telemetría del turno
  openresponses.py   Serialización del protocolo (no-streaming y SSE)
  agent.py           Loop agéntico con Claude
  mcp_server.py      Las mismas herramientas por MCP
  tools.py           Seis herramientas sobre el CV + búsqueda
  guardrails.py      Entrada (inyección), salida (PII), políticas de tema
  telemetry.py       Logging estructurado + sink de BigQuery
  config.py          Configuración por variables de entorno
  data/cv.json       Fuente de verdad
```

**El principio que ordena todo esto: cada archivo depende hacia adentro, nunca hacia afuera.**

```
main.py  ──►  agent.py  ──►  tools.py  ──►  cv.json
   │             │
   └──► openresponses.py    (no sabe nada de Claude)
   └──► guardrails.py       (no sabe nada de HTTP)
mcp_server.py ──► tools.py  (no sabe nada de Open Responses)
```

`tools.py` no sabe que existe HTTP. `openresponses.py` no sabe que existe Claude. `guardrails.py`
no sabe nada de ninguno de los dos.

**Por qué importa, con evidencia:** cuando decidimos agregar MCP, no hubo que rediseñar nada.
`mcp_server.py` importa `tools.py` y lo envuelve. **Un archivo nuevo, cero cambios en lo
existente.** Esa es la prueba de que la separación estaba bien puesta — y es un argumento mucho
más fuerte que decir "usé arquitectura limpia".

**Alternativa:** todo en un `main.py` de 800 líneas. Funcionaría igual y para un proyecto de un
fin de semana sería defendible. Pero entonces MCP habría sido cirugía, no un archivo.

---

## 5. Las decisiones de arquitectura

### 5.1 Sin base de datos vectorial

**Qué se hizo:** el CV vive como JSON estructurado con un `id` estable por entrada, y el modelo
lo consulta con seis herramientas tipadas. La búsqueda es léxica con IDF, stopwords, sinónimos
bidireccionales y normalización por longitud.

**Por qué, medido:** `evals/medir_corpus.py` compara dos bancos de preguntas — unas que comparten
vocabulario con el CV y otras parafraseadas.

| | Léxicas | Semánticas |
|---|---|---|
| recall@8 | 100% | 100% |
| recall@3 | 92% | 75% |
| MRR | 0.74 | 0.61 |

**Lo léxico no falla: rankea peor.** Y como el modelo recibe 8 candidatos y escoge, ese error de
orden no llega al usuario. Los embeddings comprarían una mejora que hoy nadie percibe.

**Qué se gana además:** trazabilidad (cada afirmación cita su `id`), determinismo (la misma
pregunta recupera el mismo contexto, lo que hace reproducible la evaluación), y cero
infraestructura extra.

**Alternativas y cuándo:**

| Opción | Cuándo la elegiría |
|---|---|
| **Vertex AI Vector Search** | Si el corpus creciera a cientos de documentos |
| **pgvector** | Si ya hubiera un Postgres en el sistema |
| **Chroma / FAISS en memoria** | Si quisiera vectores sin operar un servicio |
| **Todo el CV en el prompt** | Cabe (12,940 tokens) y con caché es barato — pero pierdes la cita por afirmación |

**Cómo sabría que cambió:** si bajo `k` a 3 para ahorrar contexto, las semánticas caen a 75%. Ese
es el umbral, y `medir_corpus.py` lo responde en segundos.

### 5.2 Servidor sin estado

**Qué se hizo:** la plataforma ofrece dos modos — reproducir el transcript completo, o
`previous_response_id` con el agente guardando el estado. Elegimos el primero.

**Por qué:** sin estado no hay sesiones que expiren, ni base de datos que respaldar, ni pegamento
para que varias instancias compartan memoria. Cloud Run escala horizontalmente sin coordinación.

**El costo:** reenviar el transcript en cada turno. A esta longitud es irrelevante, y se mitiga
con caché de prompt sobre el prefijo estable.

**El detalle elegante:** para agrupar turnos en telemetría hace falta un id de conversación. Se
resuelve sin estado — el hash del primer mensaje del usuario es estable durante toda la
conversación. La conversación misma es la memoria. Lo mismo aplica a la escalada de guardrails:
contar cuántas veces alguien insistió en un tema sale del transcript, no de una sesión.

### 5.3 Loop manual en vez del tool runner del SDK

**Qué se hizo:** el SDK de Anthropic trae un `tool_runner` que automatiza el ciclo. Escribimos el
loop a mano.

**Por qué:** dos cosas que el runner esconde y aquí hacen falta — emitir deltas de texto hacia el
cliente **mientras** corre el loop, y contabilizar herramientas, citas y tokens por turno para la
telemetría.

**Cuándo usaría el runner:** si no hubiera streaming ni telemetría por turno. Es menos código y
menos superficie para equivocarse.

### 5.4 Guardrails: deterministas solo donde son inequívocos

**El principio:** los controles por regex atajan lo barato y claro — inyección evidente, fuga de
PII, entradas absurdamente largas. El matiz vive en el prompt del sistema y se verifica en la
suite de evaluación.

**Por qué no todo por regex:** un regex agresivo rompe conversaciones legítimas, que es peor que
el problema que evita. Por eso hay pruebas **en las dos direcciones**: que bloquea los ataques
*y* que no bloquea preguntas válidas.

**Las políticas de tema no bloquean.** Detectan el tema (salario, vida privada, datos protegidos,
familia, contacto privado, confidencialidad de clientes) y le pasan al modelo la **guía de cómo
manejarlo**. La respuesta la compone él.

Un texto enlatado se siente a muro y delata que hay un filtro detrás. Una respuesta compuesta
bajo política se siente a criterio profesional. Y las guías van *después* de las preferencias del
operador porque son política, no estilo: nada configurable debe poder relajarlas.

### 5.5 Autenticación donde protege algo, y solo ahí

`/v1/responses` exige Bearer. `/mcp` queda **abierto a propósito**.

La diferencia: en `/v1/responses` cada petición gasta una llamada al modelo, así que sin auth
cualquiera que descubra la URL consume tu API key. `/mcp` no llama al modelo — solo lee un CV que
de todos modos es público, con costo cero por petición. Cerrarlo no protegería nada.

`/healthz` y la tarjeta de agente también quedan abiertos: **la plataforma necesita leer la
tarjeta sin credenciales para poder importarla.**

### 5.6 Tercera persona, a propósito

El agente habla de ti en tercera persona, no se hace pasar por ti. Quien consulta debe saber en
todo momento que habla con un agente.

Suena a detalle de tono pero es una decisión de producto: un agente que se hace pasar por el
candidato ante un reclutador es un producto peor, aunque conversacionalmente sea más vistoso.

---

## 6. Cómo se verifica que funciona

Tres capas, de más barata a más cara:

| Capa | Qué cubre | Costo | Cuándo corre |
|---|---|---|---|
| **92 pruebas offline** | Recuperación, guardrails, protocolo SSE, integridad del CV | 1 s, gratis | Cada push (CI) |
| **41 casos dorados** | Comportamiento real contra el modelo | ~1 USD, 58 s | Antes de desplegar |
| **Contrato verificado** | Formato contra el agente de referencia del reto | — | Una vez |

**Resultado de la última corrida: 41/41.** Latencia p50 7.2 s, p95 11.3 s.

**Un 100% hay que leerlo con cuidado**, y conviene que lo digas tú antes de que te lo cuestionen:
las aserciones son por subcadena, que es un listón bajo. Lo que sí prueba con solidez es lo
binario — herramientas correctas, citas correctas, y cero cifras de sueldo o teléfonos.

**Un tercio de los casos son adversariales**, y ahí está el valor: no cuando el agente sabe algo,
sino cuando no lo sabe. Kubernetes, Snowflake, AWS, una empresa donde nunca trabajaste, el
sueldo, la edad.

---

## 7. Los bugs que encontramos, y qué enseñan

Estos son buen material de demo porque muestran el proceso, no solo el resultado.

### El bug de la subcadena

La primera versión puntuaba por subcadena, así que `"con"` empataba dentro de `"Construyo"` y
`"Consultant"`. La pregunta más importante del reto —*"¿tu experiencia con modelos de
lenguaje?"*— **devolvía el puesto equivocado**. Se arregló con coincidencia por palabra completa
y stopwords. Está fijado como prueba de regresión.

### Los sinónimos direccionales

La tabla tenía la clave `llm`, pero una persona real escribe *"modelos de lenguaje"*. Esa frase
nunca llegaba a la experiencia con Gemini y Claude. Se reescribió como **grupos de equivalencia
bidireccionales**.

### El ranking plano

Conforme el CV creció, siete entradas quedaron empatadas en la misma puntuación y las entradas
que solo *mencionaban* open source ganaban a los proyectos que **eran** open source. Se arregló
con **IDF** y **normalización por longitud**.

Y al arreglarlo rompí una prueba, lo cual enseñó algo: **con un límite fijo de resultados, una
coincidencia débil no solo suma poco — desplaza a una fuerte fuera del corte.** Los falsos
positivos no son gratis cuando hay corte.

### El guardrail que estuvo muerto sin verse

Los patrones buscaban `cuanto gana` y `papas` sin tilde. Con acentos —como escribe cualquiera—
**tres de cada cuatro no se disparaban**. El modelo respondía bien igual gracias al prompt del
sistema, así que el fallo era invisible.

Eso es defensa en profundidad funcionando, y también la razón de tener las dos capas *y* pruebas:
sin pruebas, la capa rota se descubre el día que la otra falla.

### El bucle de herramientas

Un caso gastó 21 s y 7,184 tokens llamando seis veces a la misma herramienta con los mismos
argumentos. Aislé el SDK y el streaming: ambos terminaban limpio. **Era mi propio prompt** — la
guía de compensación daba una lista de compras y el modelo salía a buscar cada punto.

Se arregló en dos capas: una regla en el prompt, y un salvaguarda en código que firma cada
llamada y corta las repetidas. **Un prompt es probabilístico; un salvaguarda no.**
Resultado: 21 s → 4.3 s.

### El conjunto dorado que dejó de parsear

Al escribir unos patrones, `\b` se convirtió en un carácter de control real. YAML los rechaza,
así que **la suite entera dejó de validar** — y un eval roto no falla ruidoso, simplemente te
deja creyendo que tienes cobertura. Lo atrapó la prueba de integridad del propio conjunto.

---

## 8. Preguntas que te van a hacer

### "¿Por qué no usaste RAG con una base vectorial?"

> Lo medí. Construí dos bancos de preguntas, uno léxico y otro parafraseado, y comparé recall y
> MRR. La búsqueda léxica no pierde cobertura, pierde ranking — y como el modelo recibe ocho
> candidatos y escoge, ese error no llega al usuario. Documenté el umbral en el que cambiaría la
> decisión: si bajo k a tres, las semánticas caen a 75% y ahí sí valen los embeddings.

### "¿Cómo sabes que no alucina?"

> Por diseño no puede afirmar nada que no venga de una herramienta, y cada herramienta devuelve
> los `id` del CV que respaldan el resultado. Además hay una señal explícita: si preguntas por
> Kubernetes, la herramienta reporta que ese término no existe en ninguna parte del CV. Y un
> tercio de mi conjunto de evaluación son casos adversariales justo sobre eso.

### "¿Por qué Claude y no GPT o Gemini?"

> El trabajo aquí no es razonamiento libre, es seguir instrucciones con precisión: usar
> herramientas, no inventar, respetar políticas. Claude es fuerte ahí. Dicho eso, GPT habría sido
> la ruta con menos fricción porque su formato nativo es casi idéntico a Open Responses, y Gemini
> tendría sentido por afinidad con Google Cloud. El modelo está en una variable de entorno.

### "¿Esto escala?"

> El servidor es sin estado, así que Cloud Run escala horizontalmente sin coordinación. Lo que no
> escala es la recuperación léxica si el corpus creciera a cientos de documentos — y eso está
> aislado en un archivo, con una métrica que me diría cuándo pasó.

### "¿Cómo lo operas?"

> Cada turno emite un evento con latencia, tokens, herramientas usadas, citas y etiquetas de
> guardrail. Va a Cloud Logging y a BigQuery, y ahí lo monitoreo con el Agent Analytics Block que
> yo mismo escribí para looker-open-source. La métrica que más vigilo es la tasa de
> fundamentación: si baja, el agente está respondiendo sobre el CV sin consultarlo.

### "¿Qué harías diferente con más tiempo?"

> Un juez LLM para evaluar calidad de redacción, porque mis aserciones por subcadena son un listón
> bajo. Caché semántica, porque los reclutadores hacen las mismas cinco preguntas. Y trazas
> distribuidas con un span por llamada a herramienta, para ver dónde se va la latencia.

### "¿Y si te pregunto algo que no está en el CV?"

> Te lo dice. Esa fue la decisión de diseño más importante: un agente de CV pierde la confianza de
> un reclutador no cuando no sabe algo, sino cuando lo inventa.
