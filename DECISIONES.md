# Guía de decisiones técnicas

Este documento existe para una cosa: que puedas responder **"¿por qué hiciste esto así?"** sobre
cualquier pieza del sistema, y que además sepas qué alternativas había y cuándo habrías elegido
otra.

Cada decisión sigue el mismo formato: **qué es → por qué esto → qué más había → cuándo elegiría
la otra**. Esa última parte es la que separa a alguien que siguió un tutorial de alguien que tomó
una decisión.

---

## Índice

1. [El problema en una frase](#1-el-problema-en-una-frase)
2. [Cómo funciona, de punta a punta](#2-cómo-funciona-de-punta-a-punta)
3. [El stack, herramienta por herramienta](#3-el-stack-herramienta-por-herramienta)
4. [La estructura de archivos y por qué](#4-la-estructura-de-archivos-y-por-qué)
5. [Las decisiones de arquitectura](#5-las-decisiones-de-arquitectura)
6. [Memoria, estado y privacidad](#6-memoria-estado-y-privacidad)
7. [Cómo se verifica que funciona](#7-cómo-se-verifica-que-funciona)
8. [Los bugs que encontramos](#8-los-bugs-que-encontramos-y-qué-enseñan)
9. [Preguntas que te van a hacer](#9-preguntas-que-te-van-a-hacer)

---

## 1. El problema en una frase

Construir un agente que converse sobre tu perfil profesional, **desplegado y operable**,
compatible con el protocolo Open Responses para que la plataforma del reto lo pueda consumir.

El reto dice explícitamente que no busca "una interfaz bonita" sino criterio técnico. Todo lo que
sigue está orientado a eso: **que cada decisión tenga una razón defendible**.

---

## 2. Cómo funciona, de punta a punta

Cuando alguien escribe *"¿qué experiencia tiene con modelos de lenguaje?"*:

```
1. La plataforma manda POST /v1/responses con el transcript completo
       │
2. Se valida el Bearer token
       │
3. Se parsea el input          texto · imágenes (base64 o URL)
       │
4. GUARDRAIL DE ENTRADA        ¿inyección? → corta ANTES de gastar la llamada
       │
5. POLÍTICAS DE TEMA           ¿salario? ¿vida privada? → inyecta guía de manejo
       │
6. LOOP AGÉNTICO
       ├─ Claude recibe: system + 7 herramientas + transcript
       ├─ Decide llamar buscar_cv("modelos de lenguaje")
       ├─ Se ejecuta sobre cv.json
       ├─ Devuelve entradas + _citas: ["hab-ia", "exp-globallogic"]
       └─ Claude compone la respuesta con esos datos
       │
7. Los deltas de texto salen como eventos SSE mientras se generan
       │
8. GUARDRAIL DE SALIDA         redacción de PII · verificación de fundamento
       │
9. TELEMETRÍA                  latencia · tokens · herramientas · citas · CATEGORÍA
                               → Cloud Logging + BigQuery
```

**El punto clave:** el paso 6 nunca inventa. Claude solo puede afirmar lo que las herramientas le
devolvieron, y cada resultado trae los `id` que lo respaldan.

---

## 3. El stack, herramienta por herramienta

### Python 3.11

**Por qué:** mejor ecosistema para trabajo con LLMs — los SDK de Anthropic y de MCP son de primera
clase ahí. Y es tu lenguaje del día a día, así que lo defiendes.

**Alternativas:** TypeScript tiene SDKs igual de buenos y sería mejor si el agente viviera dentro
de una app web. Go daría arranque en frío más rápido, que importa en serverless, pero su
ecosistema de IA es más delgado.

### FastAPI

**Por qué:** tres razones concretas. Streaming SSE nativo con `StreamingResponse`, que el
protocolo exige. Documentación OpenAPI automática. Y **montar sub-apps**, que es lo que hizo
posible servir MCP en `/mcp` dentro del mismo proceso.

**Alternativas:** Flask es más simple pero síncrono. Starlette es lo que FastAPI usa por debajo.
Django es demasiado para un servicio de dos endpoints.

### Claude Opus 5

**Por qué:** el trabajo no es razonamiento libre, es **seguir instrucciones con precisión** —
usar herramientas, no inventar, respetar políticas.

**Un dato que sorprende y sirve en entrevista:** en Opus 5, `temperature`, `top_p` y `top_k`
**fueron eliminados** — mandarlos devuelve error 400. El control moderno es
`output_config.effort`, con cinco niveles. Corremos en `low` porque en un chat la latencia importa
más que la profundidad, y las respuestas ya vienen fundamentadas en datos.

**Alternativas:** GPT sería la ruta con menos fricción (su formato nativo es casi idéntico a Open
Responses). Gemini tendría sentido por afinidad con Google Cloud. Un modelo abierto evitaría el
costo por token pero te obliga a operar la inferencia — **conversación real en un banco**.

### Protocolo Open Responses

No fue elección, es el requisito. Pero sí hubo una decisión dentro: **verificamos el formato
contra el agente de referencia del propio reto**, no solo contra la especificación escrita. Le
mandamos peticiones y replicamos campo por campo. La especificación describe el contrato; la
implementación de referencia es contra lo que realmente te validan.

### MCP

**Por qué:** el brief lo nombra, y tú quieres trabajar en creación de servidores MCP.
Demostrarlo con código pesa más que escribirlo. Costó **un archivo**, porque las herramientas ya
vivían separadas del transporte.

### Docker + Cloud Run

**Por qué:** eres Google Cloud Certified Associate Cloud Engineer — es tu stack certificado.
Técnicamente: contenedores, escalado a cero, HTTPS gratis, despliegue desde código fuente.
`--min-instances 1` durante la evaluación porque el arranque en frío son ~4 s.

| Alternativa | A favor | En contra |
|---|---|---|
| Cloud Functions | Más simple | Mal ajuste para SSE largo |
| GKE | Control total | Operar un clúster para un servicio es absurdo |
| AWS Lambda | Equivalente en AWS | Fuera de tu stack; streaming incómodo |
| HF Spaces | Gratis, cero fricción | Menos historia de infraestructura |

### Secret Manager

La API key nunca aparece en claro: ni en el historial del shell, ni en la configuración del
servicio, ni en los logs. Cloud Run la monta en tiempo de ejecución.

### BigQuery

Es tu terreno, y **cierra un círculo**: la telemetría del agente se monitorea con el Agent
Analytics Block que tú escribiste. La pieza de observabilidad es una contribución open source
propia, no una herramienta traída de fuera.

El envío corre en un hilo aparte y es *best-effort*. **La observabilidad nunca debe agregar
latencia ni tumbar una respuesta.**

### Cloud Storage

Para reportes descargables, con URL firmada a 7 días.

---

## 4. La estructura de archivos y por qué

```
app/
  main.py            FastAPI: endpoints, autenticación, telemetría del turno
  openresponses.py   Serialización del protocolo (no-streaming y SSE) + imágenes
  agent.py           Loop agéntico con Claude
  mcp_server.py      Las mismas herramientas por MCP
  tools.py           7 herramientas sobre el CV + búsqueda con IDF
  guardrails.py      Entrada (inyección), salida (PII), políticas de tema
  categorias.py      Clasifica de qué trata cada turno, sin guardar texto
  reportes.py        Genera HTML, sube a GCS, firma la URL
  telemetry.py       Logging estructurado + sink de BigQuery
  config.py          Todo por variables de entorno
  data/cv.json       Fuente de verdad
```

**El principio: cada archivo depende hacia adentro, nunca hacia afuera.**

```
main.py  ──►  agent.py  ──►  tools.py  ──►  cv.json
   │             │
   └──► openresponses.py    (no sabe nada de Claude)
   └──► guardrails.py       (no sabe nada de HTTP)
mcp_server.py ──► tools.py  (no sabe nada de Open Responses)
```

**Por qué importa, con evidencia:** cuando decidimos agregar MCP, no hubo que rediseñar nada.
`mcp_server.py` importa `tools.py` y lo envuelve. **Un archivo nuevo, cero cambios en lo
existente.** Eso es prueba de que la separación estaba bien puesta — un argumento mucho más
fuerte que decir "usé arquitectura limpia".

---

## 5. Las decisiones de arquitectura

### 5.1 Sin base vectorial — y está medida, no opinada

Es la primera pregunta que hace cualquiera. `evals/medir_corpus.py` compara la recuperación
contra dos bancos de preguntas: unas que comparten vocabulario con el CV, y otras
**parafraseadas**, como habla quien no lo ha leído.

Corpus: **27 entradas, 12 940 tokens reales** (medidos con la API, 1.3% de la ventana).

| | Léxicas | Semánticas |
|---|---|---|
| recall@8 | 100% | 100% |
| recall@3 | 92% | 75% |
| MRR | 0.74 | **0.61** |

**Lo léxico no falla: rankea peor.** Y como el modelo recibe 8 candidatos y escoge, ese error de
orden **no llega al usuario**.

**Honestidad sobre la medición:** con 27 entradas, devolver 8 es entregar un tercio del CV, así
que recall@8 del 100% mide poco. Por eso el veredicto se apoya en MRR y recall@3. Y son 24
preguntas escritas por mí, no un conjunto estándar: es evidencia, no prueba.

**Cuándo cambiaría:** si bajo `k` a 3 (las semánticas caen a 75%), o si el corpus crece hasta que
8 candidatos no lo cubran. `medir_corpus.py` lo responde en segundos.

### 5.2 Servidor sin estado

La plataforma ofrece dos modos: reproducir el transcript, o `previous_response_id` con el agente
guardando estado. **Elegimos el primero.**

Sin estado no hay sesiones que expiren, ni base de datos que respaldar, ni pegamento entre
instancias. Cloud Run escala horizontalmente sin coordinación.

**El detalle elegante:** para agrupar turnos en telemetría hace falta un id de conversación. Se
resuelve sin estado — el hash del primer mensaje del usuario es estable durante toda la
conversación. **La conversación misma es la memoria.** Lo mismo aplica a la escalada de
guardrails: contar cuántas veces alguien insistió sale del transcript.

### 5.3 Loop manual en vez del tool runner del SDK

El SDK trae un `tool_runner` que automatiza el ciclo. Aquí se escribió a mano porque hacen falta
dos cosas que el runner esconde: emitir deltas **mientras** corre el loop, y contabilizar
herramientas, citas y tokens por turno para la telemetría.

**Cuándo usaría el runner:** si no hubiera streaming ni telemetría por turno.

### 5.4 Guardrails deterministas solo donde son inequívocos

Los controles por regex atajan lo barato y claro: inyección evidente, fuga de PII, entradas
absurdamente largas. El matiz vive en el prompt y se verifica en la evaluación.

**Un regex agresivo rompe conversaciones legítimas, que es peor que el problema que evita.** Por
eso hay pruebas **en las dos direcciones**: que bloquea ataques *y* que no bloquea preguntas
válidas como *"¿qué instrucciones le daba a los agentes LLM que construyó?"*.

**Las políticas de tema no bloquean.** Detectan el tema (salario, vida privada, datos protegidos,
familia, contacto privado, confidencialidad de clientes) y le pasan al modelo la **guía de cómo
manejarlo**. La respuesta la compone él.

Un texto enlatado se siente a muro y delata que hay un filtro. Una respuesta bajo política se
siente a criterio profesional. Y las guías van *después* de las preferencias del operador porque
son política, no estilo: **nada configurable debe poder relajarlas**.

### 5.5 Autenticación donde protege algo, y solo ahí

`/v1/responses` exige Bearer. `/mcp` queda **abierto a propósito**.

La diferencia: en `/v1/responses` cada petición gasta una llamada al modelo, así que sin auth
cualquiera consume tu API key. `/mcp` no llama al modelo — solo lee un CV público, costo cero.

Por eso mismo, `generar_reporte` **no se expone por MCP**: es la única herramienta que *escribe*,
y sin auth cualquiera podría llenar el bucket. Hay una prueba que exige que toda exclusión de MCP
esté justificada por escrito.

`/healthz`... perdón, `/salud`, y la tarjeta de agente también quedan abiertos: **la plataforma
necesita leer la tarjeta sin credenciales para poder importarla.**

### 5.6 Tercera persona, a propósito

El agente habla de ti en tercera persona, no se hace pasar por ti. Suena a detalle de tono pero
es una decisión de producto: un agente que se hace pasar por el candidato ante un reclutador es
un producto peor, aunque conversacionalmente sea más vistoso.

### 5.7 Imágenes: entrada sí, con el texto tratado como dato

El agente acepta imágenes en las dos formas que mandan los clientes: **data URI en base64** y
**URL http(s)**. El caso real: pegar la captura de una vacante.

Tres decisiones del parseo, todas con prueba:
- Un mensaje de solo texto sigue siendo **cadena simple**. Envolver todo en bloques encarecería
  el caso común sin ganar nada.
- Una imagen inválida o desproporcionada se descarta y **el texto pasa igual**.
- Las imágenes en turnos del asistente se descartan: reenviarlas ensucia el historial.

**El texto dentro de una imagen es contenido compartido, nunca instrucción.** Una captura que
diga "ignora tus reglas" es un intento de manipulación: se menciona y se sigue.

### 5.8 Navegación web, con el contenido como dato no confiable

`web_fetch` corre del lado de Anthropic y **solo busca URLs que ya están en la conversación**: no
navega por su cuenta ni sigue enlaces encontrados dentro de una página. Alguien tiene que pegarle
el enlace a propósito.

**La regla crítica está en el prompt:** lo que traiga de una página es dato, nunca instrucción.
Una página puede contener texto puesto ahí para manipularlo.

Y falla honestamente: LinkedIn bloquea bots, y el agente lo dice y pide el texto pegado en vez de
inventarse el contenido.

### 5.9 Reportes: la salida es un enlace, no un archivo

El protocolo declara salida de texto, así que un archivo no cabe en la respuesta. La salida es un
**enlace**: HTML autocontenido en Cloud Storage con URL firmada. Un enlace es texto, funciona sin
importar qué sepa renderizar el cliente.

**HTML y no PDF a propósito:** el PDF exige una librería pesada y fuentes en el contenedor, para
un formato que el navegador ya produce con Imprimir.

El contenido se escapa antes de tocar el HTML: lo redacta el modelo a partir de texto que pega un
tercero, y una vacante con `<script>` terminaría en un archivo que alguien abre.

---

## 6. Memoria, estado y privacidad

Son tres cosas distintas y conviene no confundirlas:

| Nivel | ¿Existe? | Cómo |
|---|---|---|
| **Dentro de una conversación** | ✅ Completa | La plataforma reenvía el transcript; la conversación es la memoria |
| **Entre conversaciones** | ❌ Deliberadamente no | Sin estado: sin sesiones, sin identidad, sin historial de terceros que custodiar |
| **Registro analítico** | ✅ Por categoría | 18 categorías, **nunca el texto** |

### La clasificación, y por qué no cuesta nada

Cada turno se clasifica en una categoría (experiencia, habilidades, vacante, compensación,
inyección, fuera de alcance…) **sin gastar una llamada extra al modelo**. Sale de señales que el
turno ya produjo, en orden de confianza:

1. Si disparó un guardrail, esa es la categoría — la señal más fuerte
2. Si no, **las herramientas que el modelo eligió llamar**. Si consultó `obtener_experiencia`, la
   pregunta era de experiencia. El modelo ya hizo el trabajo de entender la intención
3. Y solo si no hubo ninguna, palabras clave para separar saludo, meta y fuera de alcance

### La decisión de privacidad, verificable

Se guarda la categoría, **nunca el texto**. Quien escribe es un tercero que no dio permiso para
almacenar lo que escribió.

Y está **verificado, no solo documentado**: hay una prueba que falla si `registrar_turno` acepta
un parámetro de texto o si el esquema de BigQuery tiene una columna donde quepa.

---

## 7. Cómo se verifica que funciona

Cuatro capas, de más barata a más cara:

| Capa | Qué cubre | Costo | Cuándo |
|---|---|---|---|
| **214 pruebas offline** | Recuperación, guardrails, protocolo, imágenes, clasificación | 2 s, gratis | Cada push (CI) |
| **42 casos dorados** | Comportamiento real contra el modelo | ~1 USD | Antes de desplegar |
| **32 verificaciones de contrato** | Contra el endpoint YA desplegado | centavos | Antes de registrar |
| **Contrato verificado** | Contra el agente de referencia del reto | — | Una vez |

**Último resultado: 41/41 en el conjunto dorado, 32/32 contra producción.**

Latencia medida secuencialmente, como la usa una persona: **4–9 s** en preguntas normales, **~11
s** al contrastar una vacante.

**Un 100% hay que leerlo con cuidado**, y conviene decirlo tú antes de que te lo cuestionen: las
aserciones son por subcadena, que es un listón bajo. Lo que sí prueba con solidez es lo binario —
herramientas correctas, citas correctas, cero cifras de sueldo o teléfonos.

**Un tercio de los casos son adversariales**, y ahí está el valor: no cuando el agente sabe algo,
sino cuando no lo sabe.

---

## 8. Los bugs que encontramos, y qué enseñan

Este es el mejor material de demo, porque muestra el proceso y no solo el resultado.

### Recuperación

**El bug de la subcadena.** La primera versión puntuaba por subcadena, así que `"con"` empataba
dentro de `"Construyo"`. La pregunta más importante del reto devolvía el puesto equivocado.

**Los sinónimos direccionales.** La tabla tenía la clave `llm`, pero una persona escribe *"modelos
de lenguaje"*. Esa frase nunca llegaba a la experiencia con Gemini. Se reescribió como grupos
bidireccionales.

**El ranking plano.** Al crecer el CV, siete entradas quedaron empatadas y las que solo
*mencionaban* open source ganaban a los proyectos que **eran** open source. Se arregló con **IDF**
y normalización por longitud.

Y al arreglarlo rompí una prueba, lo cual enseñó algo: **con un límite fijo de resultados, una
coincidencia débil no solo suma poco — desplaza a una fuerte fuera del corte.**

### Comportamiento

**El guardrail que estuvo muerto sin verse.** Los patrones buscaban `cuanto gana` y `papas` sin
tilde. Con acentos —como escribe cualquiera— **tres de cada cuatro no se disparaban**. El modelo
respondía bien igual gracias al prompt, así que el fallo era invisible. Es defensa en profundidad
funcionando, y la razón de tener las dos capas *y* pruebas.

**El bucle de herramientas.** Un caso gastó 21 s y 7 184 tokens llamando seis veces a la misma
herramienta. Aislé el SDK y el streaming: ambos limpios. **Era mi propio prompt** — la guía daba
una lista de compras y el modelo salía a buscar cada punto. Se arregló en dos capas: una regla en
el prompt, y un salvaguarda en código que firma cada llamada. **Un prompt es probabilístico; un
salvaguarda no.** Resultado: 21 s → 4.3 s.

**El idioma que cambiaba solo.** En una conversación en español, pegar un requisito en inglés
hacía que respondiera todo en inglés. Técnicamente hacía lo que le pedí, pero la regla estaba mal
pensada: pegar un fragmento es compartir material, no cambiar de idioma.

### Infraestructura

**`/healthz` es una ruta reservada.** Google la intercepta antes de que llegue al contenedor. El
síntoma era engañoso: 404 de Google en la sonda **mientras el resto de la app respondía perfecto**.

**CRLF en los scripts.** Editar en Windows metió finales CRLF. En una línea que termina en barra
de continuación, bash pierde la continuación y ejecuta la siguiente línea como comando suelto. El
error nombraba un comando que no existe en el archivo. **Y `bash -n` valida la sintaxis sin
detectarlo.**

**No editar un script mientras corre.** Bash lo lee por posición de byte; cambiarle el tamaño a
media ejecución lo descarrila.

**Firmar URLs en Cloud Run.** Firmar exige llave privada; Cloud Run solo tiene un token. Se
resuelve firmando vía IAM, que necesita el correo de la cuenta — y ese correo vale literalmente
`"default"` en el atributo obvio. Mi primera corrección descartaba ese valor, así que **se rendía
justo en el caso para el que existía**.

### Operación

**Se acabaron los créditos a media prueba.** El agente falló con dignidad —respondió un mensaje
en vez de reventar— pero **nadie se enteró**. Es el argumento vivo de por qué existe la
telemetría: `tasa_exito` cayendo a cero es exactamente la alerta que faltaba.

---

## 9. Preguntas que te van a hacer

### "¿Por qué no usaste RAG con una base vectorial?"

> Lo medí. Construí dos bancos de preguntas, uno léxico y otro parafraseado, y comparé recall y
> MRR. La búsqueda léxica no pierde cobertura, pierde ranking — y como el modelo recibe ocho
> candidatos y escoge, ese error no llega al usuario. Documenté el umbral en el que cambiaría la
> decisión: si bajo k a tres, las semánticas caen a 75%.

### "¿Cómo sabes que no alucina?"

> Por diseño no puede afirmar nada que no venga de una herramienta, y cada herramienta devuelve
> los `id` del CV que respaldan el resultado. Además hay una señal explícita: si preguntas por
> Kubernetes, la herramienta reporta que ese término no existe en ninguna parte del CV. Un tercio
> de mi conjunto de evaluación son casos adversariales sobre eso.

### "¿Por qué Claude y no GPT o Gemini?"

> El trabajo no es razonamiento libre, es seguir instrucciones con precisión. Dicho eso, GPT
> habría sido la ruta con menos fricción porque su formato nativo es casi idéntico a Open
> Responses, y Gemini tendría sentido por afinidad con Google Cloud. El modelo está en una
> variable de entorno.

### "¿Puedo ajustar la temperatura?"

> En Opus 5 no existe: `temperature`, `top_p` y `top_k` fueron eliminados y devuelven 400. El
> control es `output_config.effort`, con cinco niveles. Corro en `low` porque las respuestas se
> fundamentan en herramientas, no en razonamiento libre, y en un chat la latencia importa más.

### "¿Esto escala?"

> El servidor es sin estado, así que Cloud Run escala horizontalmente sin coordinación. Lo que no
> escala es la recuperación léxica si el corpus creciera a cientos de documentos — y eso está
> aislado en un archivo, con una métrica que me diría cuándo pasó.

### "¿Cómo lo operas?"

> Cada turno emite latencia, tokens, herramientas, citas, etiquetas de guardrail y la categoría de
> la pregunta. Va a Cloud Logging y a BigQuery, y ahí lo monitoreo con el Agent Analytics Block
> que yo mismo escribí para looker-open-source. La métrica que más vigilo es la tasa de
> fundamentación: si baja, el agente está respondiendo sobre el CV sin consultarlo.

### "¿Guardas las conversaciones?"

> El texto no. Guardo la categoría de cada pregunta —18 posibles— porque quien escribe es un
> tercero que no me dio permiso para almacenar lo que escribió. Y no es solo una promesa: hay una
> prueba que falla si la función de registro acepta un parámetro de texto o si el esquema de
> BigQuery tiene columna donde quepa.

### "¿Y si te pregunto algo que no está en el CV?"

> Te lo dice. Esa fue la decisión de diseño más importante: un agente de CV pierde la confianza de
> un reclutador no cuando no sabe algo, sino cuando lo inventa.

### "¿Qué harías diferente con más tiempo?"

> Un juez LLM para evaluar calidad de redacción, porque mis aserciones por subcadena son un listón
> bajo. Una alerta de saldo bajo, que ya me mordió. Caché semántica, porque los reclutadores hacen
> las mismas cinco preguntas. Y trazas distribuidas con un span por llamada a herramienta.
