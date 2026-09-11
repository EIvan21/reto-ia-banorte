# Clase técnica — los conceptos detrás del agente

Este documento no describe el proyecto: enseña los conceptos que hay debajo. La
diferencia importa. Saber que el redactor de flujo retiene cuarenta y ocho
caracteres es memoria. Saber por qué un dato partido entre dos deltas no dispara
un patrón, y por tanto poder deducir cuántos caracteres hay que retener, es
entendimiento.

Está organizado en ocho módulos. Cada concepto se explica desde cero, con la
matemática cuando hace falta, y al final se conecta con dónde aparece en el
agente de CV.

---

# Módulo 1 — Cómo funciona un modelo de lenguaje, para quien lo opera

## Tokens

Un modelo de lenguaje no lee caracteres ni palabras: lee tokens. Un token es una
secuencia frecuente de caracteres que el tokenizador aprendió durante el
entrenamiento. En español, un token promedia entre tres y cuatro caracteres.
Palabras comunes son un solo token; palabras raras o con acentos se parten en
varios. El espacio antes de una palabra suele ir pegado al token.

Esto tiene tres consecuencias prácticas.

Primera: **el costo se mide en tokens, no en palabras ni en caracteres.** Una
estimación rápida para español es dividir los caracteres entre tres punto siete.
El CV de este proyecto tiene treinta y seis mil caracteres y unos doce mil
novecientos tokens.

Segunda: **el modelo no puede contar letras ni deletrear bien**, porque no ve
letras. Si le pides cuántas erres tiene "ferrocarril", está adivinando sobre una
representación que no contiene esa información directamente.

Tercera: **el mismo texto puede tokenizarse distinto según lo que lo rodea.** Esto
importa para el caché, que veremos más abajo.

## La ventana de contexto

La ventana de contexto es el número máximo de tokens que el modelo puede procesar
en una sola llamada: prompt del sistema, definiciones de herramientas, historial
de la conversación, y la respuesta que va a generar, todo junto.

Un modelo como Claude Opus 5 tiene una ventana de un millón de tokens. Eso es
mucho: el CV entero de este proyecto ocupa el uno punto tres por ciento.

**Lo que hay que entender es que la ventana no es memoria.** El modelo no recuerda
nada entre llamadas. Cada petición a la API es independiente y sin estado. La
ilusión de que un chatbot "recuerda" la conversación viene de que el cliente
reenvía el historial completo en cada llamada.

Esto es exactamente lo que hace la plataforma con este agente, y es la razón por
la que el servidor puede no guardar estado: la conversación viaja en cada
petición.

## El costo real de una ventana grande

Que quepa no significa que convenga, por tres razones distintas.

**Costo.** Se paga por token de entrada en cada llamada. Si el historial crece y
se reenvía completo, el costo por turno crece con la conversación.

**Latencia.** Procesar más tokens de entrada toma más tiempo antes de que salga
el primer token de salida.

**Atención.** Este es el que más se ignora. Los modelos no atienden uniformemente
a todo su contexto. El fenómeno se conoce como "perdido en el medio": la
información al inicio y al final del contexto se recupera mejor que la que está
en la mitad. Una ventana de un millón de tokens no garantiza que el modelo use
bien el token número quinientos mil.

Por eso, aunque el CV cabría entero en el prompt, dárselo por herramientas en
paquetes de ocho entradas relevantes no solo es más barato: puede ser más
preciso.

## Prompt caching

Es el mecanismo que más se malentiende, y en este proyecto es central.

**Cómo funciona.** Cuando mandas una petición, la API puede guardar el estado
interno correspondiente a un *prefijo* de esa petición. En la siguiente petición,
si el prefijo es **idéntico byte por byte**, el modelo no vuelve a procesarlo: lo
lee del caché.

Tres cosas críticas:

**Es por prefijo, no por contenido.** Si cambias un carácter cerca del inicio,
todo lo que sigue deja de ser un acierto de caché. Por eso el prompt del sistema
de este proyecto se mantiene byte-estable: nada de timestamps, nada de
identificadores, nada que varíe entre peticiones. Un timestamp en el prompt
invalidaría el caché en cada turno.

**El orden importa.** En la API de Anthropic el orden es: herramientas, sistema,
mensajes. Un punto de caché marcado en el bloque del sistema cachea todo lo que
viene antes, es decir también las definiciones de herramientas.

**Escribir cuesta más que leer.** Crear una entrada de caché cuesta más que un
token de entrada normal — del orden de un veinticinco por ciento más. Leer de
caché cuesta mucho menos: del orden de un diez por ciento. Así que el caché se
paga a partir de la segunda lectura.

**El TTL.** Las entradas de caché expiran. El valor predeterminado ronda los
cinco minutos, con opciones de ventanas más largas. Cada acierto suele refrescar
el reloj. Esto importa para una conversación donde la persona tarda mucho entre
mensajes: si pasa el TTL, el siguiente turno paga precio completo y vuelve a
escribir el caché.

**Los puntos de caché múltiples.** Puedes marcar más de un punto. El patrón para
conversaciones es: uno en el prefijo estable — sistema y herramientas — y otro
que se va moviendo al final del historial ya fijo. El segundo captura la
conversación acumulada.

**Por qué en el penúltimo mensaje y no en el último.** El último mensaje es la
pregunta nueva de este turno: cambia siempre, así que un punto ahí nunca tendría
un acierto. Todo lo anterior sí se repite palabra por palabra en el siguiente
turno.

**Y por qué los puntos viejos siguen sirviendo.** El caché funciona por prefijo,
así que una entrada creada para el prefijo P se acierta con cualquier petición
que empiece con P. Poner un punto nuevo más adelante crea una entrada nueva sin
invalidar la anterior. Es caché incremental.

## Los parámetros de muestreo, y por qué desaparecieron

Un modelo de lenguaje, en cada paso, produce una distribución de probabilidad
sobre todos los tokens posibles. Los parámetros de muestreo controlan cómo se
escoge el siguiente token de esa distribución.

**Temperature** escala la distribución antes de muestrear. Con temperatura cero se
toma siempre el token más probable, lo cual da salida determinista y repetitiva.
Con temperatura alta la distribución se aplana y tokens improbables tienen más
chance, lo cual da salida más variada y más errática.

**Top-p**, también llamado muestreo de núcleo, se queda con los tokens más
probables hasta que su probabilidad acumulada llega a p, y muestrea solo entre
ellos.

**Top-k** se queda con los k tokens más probables.

**Claude Opus 5 eliminó los tres.** Esto es un dato que vale la pena tener claro,
porque si alguien te dice "baja la temperatura para que invente menos", está
describiendo un modelo anterior. Además, esa creencia siempre fue parcialmente
falsa: temperatura cero no elimina las alucinaciones, solo las vuelve
determinísticas — el modelo inventa lo mismo cada vez.

**Lo que queda es el esfuerzo.** Es un control de cuánto razonamiento interno
hace el modelo antes de responder. Más esfuerzo suele dar mejores respuestas en
problemas que requieren razonamiento encadenado, a cambio de latencia y tokens.

En este agente el esfuerzo se fija en bajo, y el argumento es que el cuello de
botella no es razonar: las respuestas se fundamentan en herramientas sobre un
corpus pequeño, y razonar más profundo no encuentra un dato que no está en el
archivo. Medido: la misma pregunta tarda ocho punto tres segundos con esfuerzo
bajo y catorce punto cuatro con esfuerzo alto.

## Tool use, o llamada a funciones

Es el mecanismo que convierte un modelo en un agente.

**Cómo funciona.** Le mandas al modelo, junto con los mensajes, una lista de
herramientas. Cada herramienta tiene nombre, descripción y un esquema JSON de sus
parámetros. El modelo puede responder con texto, o puede responder con un bloque
de tipo `tool_use` que contiene el nombre de la herramienta y los argumentos que
quiere pasarle.

**Punto crítico: el modelo no ejecuta nada.** Solo dice qué quiere que se
ejecute. Tu código recibe esa petición, ejecuta la función de verdad, y le
devuelve el resultado en un bloque `tool_result` como si fuera un mensaje más del
usuario. Entonces el modelo continúa.

Ese ida y vuelta es el loop agéntico.

**La descripción de la herramienta es prompt.** El modelo decide cuándo llamar
cada herramienta leyendo su descripción. Una descripción vaga produce llamadas
erráticas. En este proyecto, la descripción de la herramienta de reportes le
prohíbe explícitamente usarla por iniciativa propia, y eso es lo único que impide
que ofrezca archivos que nadie pidió.

**Strict mode y JSON Schema.** El esquema de parámetros usa JSON Schema. En modo
estricto, con `additionalProperties: false` y todos los campos en `required`, la
API garantiza que los argumentos que produce el modelo cumplen el esquema. En
este proyecto todos los parámetros son obligatorios, incluso los opcionales, que
se pasan como cadena vacía. Eso elimina una clase entera de errores donde el
modelo omite un campo y la llamada falla.

## stop_reason: el contrato de control

Cada respuesta del modelo trae un `stop_reason` que dice por qué terminó. Es la
máquina de estados del loop.

`end_turn`: terminó normalmente. Devuelve el texto.

`tool_use`: quiere ejecutar herramientas. Ejecútalas y vuelve a llamar.

`max_tokens`: se acabó el presupuesto de salida a media respuesta. Hay que
cerrar el stream limpiamente; la respuesta queda truncada.

`refusal`: el modelo declinó por política. Aquí hay que dar un mensaje seguro,
no reintentar en bucle.

`pause_turn`: una herramienta del lado del servidor agotó su presupuesto y la API
pide que continúes la llamada. Se vuelve a llamar con el mismo historial.

Un loop que solo mira `tool_use` y asume que todo lo demás es texto funciona
noventa por ciento de las veces y falla raro el otro diez.

## Streaming y deltas

En modo streaming, la API no devuelve la respuesta completa: va emitiendo eventos
conforme genera. Un `delta` es un fragmento de texto — puede ser un token, puede
ser varios.

**La consecuencia que importa para seguridad:** una vez que emitiste un delta, ya
salió. No puedes retractarlo. Cualquier control que quieras aplicar sobre la
salida tiene que aplicarse **antes** de emitir, no al final.

Ese es exactamente el bug que tuvo este proyecto: la redacción de datos
personales se aplicaba al texto final, cuando los deltas ya habían salido.

---

# Módulo 2 — Recuperación: la teoría completa

## El problema

Tienes un corpus: un conjunto de documentos. Tienes una consulta. Quieres
devolver los k documentos más relevantes. Ese es el problema de recuperación de
información, y tiene sesenta años de literatura.

En el contexto de agentes, se le llama RAG — generación aumentada por
recuperación. La idea es que en vez de confiar en lo que el modelo memorizó
durante el entrenamiento, le das documentos recuperados y le pides que responda
basándose en ellos.

**Vocabulario básico.** El *corpus* es el conjunto completo. Un *documento* es
una unidad recuperable. El *chunk* es cómo partes un documento grande en piezas
del tamaño adecuado. La *consulta* es lo que el usuario pregunta. El *índice* es
la estructura que permite buscar rápido.

En este proyecto el corpus son treinta entradas del CV, y no hay chunking porque
cada entrada ya tiene el tamaño adecuado.

## Búsqueda léxica: el índice invertido

La búsqueda léxica empata palabras. La estructura clásica es el índice invertido:
un diccionario que va de cada término a la lista de documentos que lo contienen.

Para responder una consulta, tomas sus términos, recuperas las listas de
documentos de cada uno, y las combinas.

**El problema de contar apariciones.** Si simplemente cuentas cuántos términos de
la consulta aparecen en cada documento, pasan dos cosas malas.

Primera, los términos comunes dominan. La palabra "de" aparece en todos los
documentos y no distingue nada, pero suma igual que un término raro.

Segunda, los documentos largos ganan siempre, porque tienen más palabras y por
tanto más probabilidad de contener cualquier término.

Esos dos problemas tienen solución y son TF-IDF y la normalización por longitud.

## TF-IDF

**TF** es frecuencia de término: cuántas veces aparece el término en el
documento. La intuición es que si un documento menciona "LookML" cinco veces,
probablemente trata de LookML.

**IDF** es frecuencia inversa de documento, y es la parte interesante. Mide qué
tan raro es un término en el corpus completo.

La fórmula estándar es:

    IDF(t) = log(N / df(t))

donde N es el número total de documentos y df(t) es en cuántos documentos aparece
el término t.

**Hagamos el cálculo con números de este proyecto.** El corpus tiene treinta
entradas.

Si un término aparece en las treinta entradas, su IDF es log(30/30) = log(1) = 0.
No aporta nada, y matemáticamente su contribución al puntaje es cero. Eso es lo
que pasa con "de" y "en".

Si un término aparece en tres entradas, su IDF es log(30/3) = log(10) ≈ 2.3.

Si aparece en una sola entrada, log(30/1) = log(30) ≈ 3.4. Es el término más
distintivo posible: si la consulta lo trae, casi con seguridad quiere ese
documento.

**El puntaje TF-IDF** de un documento para una consulta es la suma, sobre los
términos de la consulta, de TF por IDF.

**Un detalle de implementación en este proyecto:** el IDF se topa en tres punto
cero. La razón es que un término que no aparece en ningún documento del corpus no
tiene IDF definido — la división sería por cero — y conviene que pese como algo
raro pero acotado, no como infinito.

## Normalización por longitud

Aunque uses IDF, un documento largo sigue teniendo ventaja porque acumula más
coincidencias. La solución es dividir el puntaje por alguna función de la
longitud del documento.

En este proyecto ese ajuste se agregó después de un fallo concreto: siete
entradas empataban con el mismo puntaje, y las entradas narrativas largas le
ganaban a los proyectos reales, que son más cortos pero más específicos.

**BM25** es la fórmula estándar moderna que hace todo esto bien. Es TF-IDF con
dos refinamientos: la frecuencia de término se satura — la quinta aparición de un
término aporta menos que la segunda — y la normalización por longitud tiene un
parámetro ajustable. Es el algoritmo que usan Elasticsearch y Lucene por
omisión. Cuando alguien dice "búsqueda híbrida BM25 más embeddings", BM25 es esta
parte.

## Stopwords, stemming y lematización

**Stopwords** son palabras vacías: artículos, preposiciones, conjunciones. Se
eliminan porque no distinguen. En este proyecto la lista incluye español e inglés,
y su ausencia causó el primer bug serio: la palabra "con" empataba dentro de
"Construyo" y de "Consultant", y la pregunta más importante del reto devolvía el
empleo equivocado.

Nota que ahí hay dos problemas distintos: uno es que "con" es una stopword, y
otro es que se estaba haciendo coincidencia de subcadena en vez de coincidencia
de palabra completa. Los dos se arreglaron.

**Stemming** recorta las palabras a una raíz aproximada con reglas. "Corriendo",
"corrió" y "correr" pueden llegar todas a "corr". Es rápido y bruto: a veces
produce raíces que no son palabras.

**Lematización** hace lo mismo pero con un diccionario, devolviendo la forma
canónica real: "corriendo" a "correr". Es más preciso y más caro.

Este proyecto no usa ninguno de los dos formalmente. Usa una función de variantes
morfológicas que cubre dos casos: una palabra extiende a la otra — modelo y
modelos — o ambas comparten una raíz de al menos cinco caracteres con ambas
palabras de seis o más — revisión y revisor.

El comentario del código explica el criterio, que es una buena manera de pensar
estas cosas: un falso positivo solo agrega una entrada floja que el modelo
descarta, mientras que un falso negativo pierde la respuesta correcta por
completo. Los costos no son simétricos, así que se prefiere recuperar de más.

## Expansión de consulta con sinónimos

El problema fundamental de la búsqueda léxica es el desajuste de vocabulario: el
usuario y el documento usan palabras distintas para la misma cosa.

La solución léxica es expandir la consulta. Si el usuario dice "modelos de
lenguaje", agregas "LLM", "Gemini", "Claude" a los términos de búsqueda.

**Un detalle de diseño que este proyecto aprendió a la mala.** La primera versión
usaba una tabla direccional: la clave era "llm" y sus valores eran los sinónimos.
Eso funciona si el usuario escribe "llm", pero falla si escribe "modelos de
lenguaje", porque esa frase no es la clave. Se reescribió como grupos de
equivalencia: si la consulta toca cualquier miembro del grupo, se expande a todo
el grupo.

## Búsqueda semántica: embeddings

Un embedding es un vector de números — típicamente de varios cientos o miles de
dimensiones — que representa el significado de un texto. Lo produce un modelo
entrenado para que textos con significado parecido queden cerca en ese espacio.

**Cerca en qué sentido.** La medida estándar es similitud coseno: el coseno del
ángulo entre los dos vectores. Vale uno si apuntan en la misma dirección, cero si
son perpendiculares, menos uno si son opuestos. Se usa el coseno y no la
distancia euclidiana porque lo que importa es la dirección del vector, no su
magnitud.

**Cómo se usa para recuperar.** Calculas el embedding de cada documento una vez y
los guardas. Cuando llega una consulta, calculas su embedding y buscas los
documentos cuyos vectores estén más cerca. Eso es una base de datos vectorial:
una estructura que hace esa búsqueda rápido sobre millones de vectores, usando
índices aproximados como HNSW o IVF.

**Por qué gana.** Porque no necesita palabras compartidas. "Almacén de datos en
la nube" puede recuperar un documento sobre BigQuery aunque no compartan ni una
palabra.

**Por qué a veces no gana.** Tres razones que hay que poder decir.

Primera: con un corpus pequeño, la diferencia se diluye. Si tienes treinta
documentos y devuelves ocho, estás devolviendo más de un cuarto del corpus; casi
cualquier método decente va a incluir el correcto.

Segunda: los embeddings son borrosos con nombres propios y términos técnicos
exactos. "Looker" y "Tableau" están cerca en el espacio semántico porque ambos
son herramientas de BI, pero para un CV son cosas completamente distintas. La
búsqueda léxica no confunde una con otra.

Tercera: introducen infraestructura. Hay que generar embeddings, guardarlos,
mantenerlos sincronizados cuando el corpus cambia, y pagar el servicio.

**El resultado en este proyecto.** Se midió con veinticuatro preguntas partidas
en dos grupos: las que comparten vocabulario con el CV y las que dicen lo mismo
con otras palabras. El segundo grupo es donde los vectores deberían ganar. La
brecha en MRR fue de cero punto cero uno.

## Las métricas de recuperación

Aquí está la matemática, porque te la pueden pedir.

**Recall arroba k.** De todas las consultas de prueba, en qué fracción el
documento correcto aparece entre los primeros k resultados. Si tienes cien
consultas y en noventa y dos el correcto está en el top tres, tu recall arroba
tres es noventa y dos por ciento.

Mide cobertura: ¿está ahí? No mide si está arriba.

**Precision arroba k.** De los k resultados devueltos, qué fracción son
relevantes. Si devuelves ocho y dos son relevantes, precision arroba ocho es
veinticinco por ciento.

En RAG, precision importa menos de lo que parece, porque el modelo filtra: le
puedes dar ocho candidatos y que use dos. Por eso este proyecto optimiza recall,
no precision.

**MRR, rango recíproco medio.** Para cada consulta, tomas la posición del primer
resultado relevante y calculas su recíproco: uno dividido por la posición. Si el
correcto salió primero, uno. Si salió segundo, cero punto cinco. Si salió
tercero, cero punto treinta y tres. Si salió octavo, cero punto ciento
veinticinco. Luego promedias sobre todas las consultas.

Un MRR de cero punto sesenta y ocho significa, aproximadamente, que en promedio
el resultado correcto sale entre la primera y la segunda posición.

MRR mide calidad del orden, no solo presencia. Por eso es la métrica que de
verdad separa dos recuperadores cuando ambos tienen recall alto.

**NDCG, ganancia acumulada descontada normalizada.** Es MRR generalizado a casos
donde hay varios documentos relevantes con distintos grados de relevancia. Cada
posición tiene un descuento logarítmico. Es la métrica estándar en búsqueda web.
Este proyecto no la usa porque cada pregunta tiene un conjunto pequeño de
respuestas aceptables, no un ranking graduado.

## Reranking

El patrón de dos etapas de los sistemas de recuperación modernos.

Primera etapa: un recuperador rápido y barato — BM25, o búsqueda vectorial —
devuelve muchos candidatos, digamos cien.

Segunda etapa: un modelo más caro y más preciso — típicamente un cross-encoder,
que ve la consulta y el documento juntos en vez de comparar vectores calculados
por separado — reordena esos cien y devuelve los diez mejores.

Funciona porque el modelo caro solo corre sobre cien documentos, no sobre el
corpus entero.

Es la pieza que este proyecto agregaría si el corpus creciera, porque ataca
exactamente el problema que tiene: el orden, no la cobertura.

## Búsqueda híbrida

Correr búsqueda léxica y búsqueda semántica en paralelo y fusionar los
resultados. La forma estándar de fusionar se llama fusión de rango recíproco:
cada documento recibe un puntaje que es la suma, sobre los dos rankings, de uno
sobre una constante más la posición.

Funciona porque los dos métodos fallan de maneras distintas. El léxico falla
cuando el vocabulario no coincide; el semántico falla con nombres propios y
términos exactos. Juntos se cubren.

## Chunking

Cuando los documentos son grandes, hay que partirlos. Las decisiones son: de qué
tamaño, con cuánto traslape entre chunks, y si se parte por caracteres, por
oraciones o por estructura.

El problema clásico: un chunk demasiado chico pierde el contexto que le da
sentido; uno demasiado grande diluye la señal y mete ruido en el prompt.

Las técnicas modernas incluyen chunking con metadatos — cada chunk lleva de qué
documento y sección viene — y chunking contextual, donde a cada chunk se le
antepone un resumen generado de dónde encaja en el documento completo.

Este proyecto no chunkea porque el CV está estructurado en JSON: cada entrada ya
es una unidad semántica del tamaño correcto. Eso es una ventaja de tener el
corpus estructurado y no en prosa.

---

# Módulo 3 — Agentes

## Qué distingue a un agente

Un **chatbot** recibe un mensaje y responde texto. Un **pipeline** ejecuta pasos
fijos en orden. Un **agente** decide, en tiempo de ejecución, qué acciones tomar
para cumplir un objetivo, y esas acciones pueden cambiar según lo que va
descubriendo.

La diferencia práctica es el loop: el agente puede llamar una herramienta, ver el
resultado, y decidir con base en eso si llama otra, cuál, y con qué argumentos.

## El loop agéntico como máquina de estados

El loop tiene estados y transiciones, y pensarlo así ayuda a no dejar huecos.

Estado inicial: llamar al modelo con el historial.

Del resultado salen las transiciones: si pidió herramientas, ejecutarlas y
volver al estado inicial con los resultados agregados; si terminó, salir con el
texto; si rechazó, salir con mensaje seguro; si pausó, volver al estado inicial
sin agregar nada.

**Las tres cosas que un loop de producción necesita y que un tutorial omite.**

Un tope de iteraciones. Sin él, un modelo que insiste en llamar herramientas es
un bucle infinito.

Un timeout total del turno, independiente del tope de iteraciones, porque seis
llamadas lentas suman.

Manejo de errores de herramienta que devuelva el error al modelo como resultado,
en vez de tumbar el turno. El modelo suele recuperarse: ve el error y cambia de
estrategia.

## Grounding, o fundamentación

Grounding significa que las afirmaciones del modelo estén ancladas en una fuente
verificable, no en su memoria paramétrica — lo que aprendió durante el
entrenamiento.

**Por qué importa tanto aquí.** El modelo sabe cosas sobre el mundo, incluyendo
cómo se ven los CVs de ingenieros mexicanos. Si le preguntas por el título
universitario de alguien y no tiene el dato, la salida más probable
estadísticamente es un título plausible, no un "no sé". Los modelos están
entrenados para continuar texto de forma coherente, y "no lo sé" es una
continuación menos probable que un título que suene bien.

**Las citas son el mecanismo.** Si cada afirmación se puede rastrear a un
identificador del corpus, puedes verificar. Sin citas, no hay nada que verificar.

Por eso este proyecto no mete el CV en el prompt aunque quepa: no es un problema
de costo, es que sin herramientas no hay citas.

## Tipos de alucinación

No todas son iguales, y distinguirlas ayuda a defenderse de cada una.

**Alucinación de hecho.** El modelo afirma algo falso que suena plausible. Es la
que ocurrió en este proyecto con el título universitario. Se combate con
grounding y con verificación posterior.

**Alucinación de atribución.** El modelo dice algo cierto pero se lo atribuye a la
fuente equivocada. Se combate verificando que lo afirmado esté en lo citado — que
es exactamente el pendiente documentado de este proyecto.

**Sobre-generalización.** El modelo infiere de "Docker" que hay "Kubernetes",
porque en su entrenamiento van juntos. Este proyecto lo ataca explícitamente en
el prompt: nunca convertir una tecnología ausente en presente por parecerse a
otra que sí está.

**Alucinación de juicio.** El modelo emite una evaluación que ninguna fuente le
dio. En este proyecto ocurrió: el agente dijo que el candidato "no es tan fuerte
en IA". Ninguna herramienta le dio eso. Es la misma falta que inventar un dato,
en versión evaluativa, y es más difícil de detectar porque no hay un hecho que
contrastar.

## Memoria: las cuatro clases

La taxonomía viene de la psicología cognitiva y se usa tal cual en la literatura
de agentes.

**Memoria de trabajo, o de corto plazo.** Lo que el modelo puede ver ahora: la
ventana de contexto. En este agente, el transcript que reenvía la plataforma más
los resultados de herramientas del turno.

**Memoria semántica.** Hechos. "Edher estudió Ingeniería en Energía." En este
agente es el CV más las herramientas de recuperación. Es memoria de largo plazo,
solo que autorizada por una persona en vez de aprendida.

**Memoria episódica.** Interacciones pasadas. "Este reclutador preguntó por
Looker la semana pasada." Este agente **no la tiene**, y es una decisión.

**Memoria procedimental.** Cómo actuar. El prompt del sistema y las herramientas
disponibles.

**Lo que cambió con las ventanas grandes.** La distinción corto plazo / largo
plazo era más load-bearing cuando las ventanas eran de cuatro mil tokens y
literalmente no cabía la conversación. Con un millón de tokens, el problema
dejó de ser de espacio y pasó a ser de precisión: qué eliges *no* poner en el
contexto para que el modelo atienda a lo que importa.

## Por qué resumir hechos es peligroso

Este es el argumento más sutil del proyecto y vale la pena tenerlo claro.

El patrón estándar de memoria para agentes es el buffer con resumen: cuando la
conversación pasa de N mensajes, llamas a un modelo para que resuma los más
viejos, y mandas el resumen más los recientes.

Funciona bien para conversación general. **Es peligroso para hechos
verificables**, porque un resumen es una paráfrasis, y una paráfrasis pierde
precisión. "Licenciado en Ingeniería en Energía por la Universidad Autónoma
Metropolitana, 2016 a 2021" puede resumirse a "ingeniero titulado", y a partir de
ahí el modelo rellena los huecos con lo que suena plausible.

Es literalmente el mecanismo de la alucinación que este proyecto observó.

**La distinción que salva el concepto:** resumir *hechos sobre la persona* está
prohibido; resumir *lo que quiere el interlocutor* es seguro. "Busca un rol de
Looker, preguntó por Kubernetes, le importa producción" no es una afirmación
sobre el candidato y no puede inventarle un título.

**Y la alternativa que este proyecto sí usa:** cachear en vez de resumir. El
caché reduce el costo del contexto sin perder un solo carácter. Es la única
optimización de memoria compatible con una garantía de no inventar.

---

# Módulo 4 — Concurrencia, el event loop, y por qué importa

## El problema que resuelve la asincronía

Un servidor web atiende muchas peticiones. Cada petición de este agente pasa
entre cinco y quince segundos **esperando** a que la API del modelo responda. Ese
tiempo el CPU no hace nada.

Hay dos formas de aprovecharlo.

**Un hilo por petición.** Cada petición corre en su propio hilo del sistema
operativo. Cuando un hilo se bloquea esperando, el sistema operativo pone a
correr otro. Funciona, pero cada hilo cuesta memoria — del orden de megabytes de
stack — y cambiar entre hilos tiene costo. Miles de hilos se vuelven caros.

**Un solo hilo con event loop.** Un solo hilo atiende todas las peticiones,
alternando entre ellas cada vez que una se pone a esperar. Es lo que hacen
Node.js, y Python con asyncio.

## Qué es realmente el event loop

Es un bucle que mantiene una cola de tareas listas para avanzar. Toma una, la
ejecuta hasta que cede el control, la guarda, toma la siguiente.

**La clave está en "hasta que cede el control".** Una tarea cede el control
cuando llega a un `await` sobre algo que todavía no está listo — una respuesta de
red, un temporizador. En ese momento el event loop registra "avísame cuando esto
esté" y pasa a otra tarea.

**Y aquí está el peligro.** Si una tarea **no** cede el control — porque está
calculando, o porque llamó a una función bloqueante que no es asíncrona — el
event loop no puede hacer nada más. Todas las demás peticiones se quedan
congeladas hasta que esa termine.

Un solo hilo bloqueado bloquea el servidor entero.

## async, await, y el error clásico

Marcar una función como `async` no la hace concurrente. Lo que la hace
concurrente es que **adentro** ceda el control con `await` sobre operaciones que
de verdad son asíncronas.

El error clásico — y el que tuvo este proyecto — es escribir una función `async`
que llama a una librería síncrona. Una librería síncrona hace una llamada de red
bloqueante: se queda ahí, ocupando el hilo, sin ceder nada.

En este proyecto, la ruta sin streaming era una corrutina que llamaba al cliente
síncrono de Anthropic. Mientras esos ocho segundos corrían, nada más avanzaba en
esa instancia: ni otra conversación, ni el endpoint de salud que Cloud Run
consulta para decidir si el contenedor sigue vivo.

**La solución: el threadpool.** Cuando tienes que llamar código bloqueante desde
código asíncrono, lo mandas a un pool de hilos separado. La corrutina hace `await`
sobre el resultado del hilo, lo cual cede el control correctamente, y el bloqueo
ocurre en un hilo que a nadie le importa.

En Starlette y FastAPI eso es `run_in_threadpool`.

**Un detalle que vale la pena saber:** FastAPI ya hace esto automáticamente para
funciones de ruta declaradas con `def` en vez de `async def`. Las manda al
threadpool solas. El problema aparece justo cuando declaras `async def` y adentro
llamas algo bloqueante, porque ahí le dijiste al framework que tú te encargas.

**Y el caso que sí estaba bien en este proyecto:** la ruta de streaming.
`StreamingResponse` detecta que el generador es síncrono y lo itera en threadpool
por su cuenta.

## ASGI, uvicorn y workers

**ASGI** es la especificación de interfaz entre un servidor web y una aplicación
Python asíncrona. Es el sucesor de WSGI, que era síncrono y no soportaba
streaming ni websockets.

**Uvicorn** es el servidor ASGI que corre esta aplicación.

**Workers** son procesos. Cada worker tiene su propio event loop. Más workers dan
paralelismo real de CPU, a costa de más memoria.

Este proyecto corre con **un solo worker**, y el razonamiento es el correcto para
esta carga: el trabajo es esperar a la API del modelo, no calcular. Un event loop
atiende muchas esperas sin problema. Más workers gastarían memoria sin agregar
capacidad útil.

---

# Módulo 5 — Protocolos

## HTTP request/response, y su límite

El modelo clásico: el cliente manda una petición, el servidor responde una vez,
la conexión se cierra. Para una respuesta que tarda quince segundos en generarse,
el usuario ve una pantalla vacía quince segundos.

## Server-Sent Events

SSE resuelve eso. Es un estándar web donde el servidor mantiene la conexión
abierta y va mandando eventos conforme los tiene. El cliente los recibe uno por
uno.

**El formato de cable**, que conviene conocer porque las pruebas de este proyecto
lo verifican:

    event: response.output_text.delta
    data: {"type":"response.output_text.delta","delta":"Hola"}

    event: response.completed
    data: {"type":"response.completed",...}

    data: [DONE]

Cada evento es un bloque de líneas. La línea `event:` nombra el tipo. La línea
`data:` lleva el contenido, típicamente JSON. Un bloque termina con una línea en
blanco. El content-type es `text/event-stream`.

**El terminador.** La convención que heredó de OpenAI es una línea final
`data: [DONE]`. No es parte del estándar SSE; es del protocolo de la aplicación.
Sirve para que el cliente sepa que terminó bien y no por una conexión cortada.

**Por qué SSE y no WebSockets.** SSE es unidireccional — servidor a cliente — y
va sobre HTTP normal, así que atraviesa proxies y balanceadores sin
configuración especial. WebSockets es bidireccional y más potente, pero requiere
un handshake de actualización de protocolo y más infraestructura. Para "el
servidor manda texto conforme lo genera", SSE es exactamente lo que hace falta.

## Open Responses

Es el protocolo que el reto exige. Define la forma de la petición — un campo
`input` que puede ser una cadena o una lista de mensajes — y la forma de la
respuesta.

Los puntos que este proyecto verifica con pruebas: que el objeto tenga
`object: "response"`, que el contenido de texto venga en bloques de tipo
`output_text`, que el campo `error` sea explícitamente nulo cuando no hay error —
no ausente —, y que la secuencia de eventos SSE sea la correcta y termine con el
terminador.

**Una decisión de diseño de este proyecto:** cuando la autenticación falla, no se
devuelve un 401 con un JSON cualquiera. Se devuelve un objeto válido de Open
Responses con `status: "failed"` y un campo `error` poblado. Un cliente que
espera ese protocolo puede parsear el fallo igual que el éxito.

**El principio general** se llama la ley de Postel, o principio de robustez: sé
liberal en lo que aceptas y estricto en lo que emites. Este proyecto acepta tanto
`"input": "hola"` como la lista de items de mensaje, porque distintos clientes
mandan ambas.

## MCP, Model Context Protocol

Es un protocolo abierto para que un agente descubra y use herramientas expuestas
por otro proceso. La analogía útil: si las herramientas son funciones, MCP es la
convención de llamada entre procesos.

**Qué define.** Cómo un cliente descubre qué herramientas hay — cada una con
nombre, descripción y esquema —, cómo las invoca, y cómo se devuelven los
resultados. También cubre recursos y prompts, además de herramientas.

**Transportes.** Dos principales. **stdio**, donde el servidor es un proceso local
y la comunicación va por entrada y salida estándar; es como se conecta a
aplicaciones de escritorio. Y **HTTP con streaming**, donde el servidor es un
endpoint web.

Este proyecto soporta los dos: montado en `/mcp` dentro de la misma aplicación
FastAPI, y como proceso local con `python -m app.mcp_server`.

**Protección contra DNS rebinding.** Un servidor MCP sobre HTTP tiene que validar
el encabezado `Host`, porque un sitio malicioso podría hacer que el navegador de
un usuario apunte a un servidor local. Por eso el transporte trae configuración
de seguridad.

**Por qué exponer MCP aquí demuestra algo.** No es una característica de usuario:
es una demostración de arquitectura. Las herramientas viven en un módulo que no
sabe nada del transporte, así que agregar un segundo protocolo fue un archivo
nuevo, no un rediseño. Si las herramientas hubieran vivido dentro del loop del
agente, habría sido imposible.

## La tarjeta de agente

Un archivo JSON en una ruta convenida — `/.well-known/agent-card.json` — donde un
agente se describe a sí mismo: nombre, descripción, capacidades, modos de entrada
que acepta, habilidades, y las interfaces que soporta con sus URLs y protocolos.

El patrón `/.well-known/` es un estándar de IETF para ubicaciones de metadatos
conocidas; es el mismo mecanismo que usan `robots.txt` o los descubrimientos de
OAuth.

Tiene que servirse **sin autenticación**, porque quien lo lee todavía no tiene
credenciales: apenas está descubriendo qué es el agente.

---

# Módulo 6 — Seguridad de aplicaciones con modelos de lenguaje

## Por qué es un dominio distinto

En seguridad de software clásica, código y datos están separados. En una
aplicación con modelo de lenguaje, **todo es texto en el mismo canal**: las
instrucciones del sistema, lo que escribe el usuario, lo que devuelve una
herramienta, lo que trae una página web. El modelo los distingue por convención,
no por arquitectura.

Eso hace que la inyección de prompt no tenga una solución completa, a diferencia
de la inyección SQL, que se resuelve con consultas parametrizadas porque ahí sí
hay separación real entre código y datos.

## OWASP Top 10 para aplicaciones de modelos de lenguaje

**LLM01, inyección de prompt.** Manipular el comportamiento del modelo con
entradas. Se divide en directa — el usuario lo escribe — e indirecta — el texto
viene de una fuente que el modelo lee: una página, un documento, una imagen, un
turno fabricado del historial.

La indirecta es la peligrosa, porque la víctima no sabe que ocurrió. Un atacante
pone texto en una página; el usuario le pide al agente que lea esa página; el
agente obedece el texto.

**LLM02, divulgación de información sensible.** Que el modelo revele datos que no
debía. Incluye datos del prompt del sistema, datos de otros usuarios, y datos
personales del corpus.

**LLM03, cadena de suministro.** Modelos, datasets o plugins de terceros
comprometidos.

**LLM04, envenenamiento de datos.** Manipular los datos de entrenamiento o de
ajuste fino para introducir comportamiento malicioso.

**LLM05, manejo inadecuado de salidas.** Confiar en la salida del modelo sin
validarla. El caso clásico: el modelo genera HTML o SQL y la aplicación lo
ejecuta sin sanitizar. En este proyecto aplica a la redacción de datos personales
antes de emitir.

**LLM06, agencia excesiva.** Darle al modelo más permisos de los que necesita. Si
el agente puede borrar archivos, una inyección exitosa puede borrar archivos. El
principio es el de mínimo privilegio.

**LLM07, fuga del prompt del sistema.** Que se pueda extraer el prompt. El
mitigante real no es solo impedirlo: es no poner secretos en el prompt, porque
asumir que el prompt es extraíble es la postura segura.

**LLM08, debilidades de vectores y embeddings.** Ataques sobre el almacén
vectorial de un sistema RAG: envenenar el índice, o extraer documentos por
inferencia.

**LLM09, desinformación.** Que el modelo produzca información falsa que el
usuario crea. Es la categoría central de este proyecto.

**LLM10, consumo no acotado.** Que alguien pueda hacer que gastes sin límite:
peticiones sin autenticación, prompts enormes, loops sin tope.

## Por qué el regex no basta, y por qué igual se usa

Un patrón de expresión regular para inyección detecta las formas que conoce. Un
atacante las varía: cambia el idioma, usa sinónimos, codifica en base64, parte la
frase, la esconde en una imagen.

Entonces, ¿para qué sirve? Para tres cosas.

**Atajar lo barato.** Los intentos evidentes son la mayoría del tráfico
malicioso, y cortarlos antes de llamar al modelo ahorra dinero y latencia.

**Dar una señal.** Aunque no bloquees, saber que alguien lo intentó es
información operativa.

**Ser una capa, no la capa.** La defensa en profundidad significa que ninguna
capa tiene que ser perfecta.

**El riesgo del regex agresivo**, que este proyecto trata explícitamente: romper
conversaciones legítimas es peor que el problema que evitas. Un patrón que
bloquea "qué instrucciones le daba a los agentes que construyó" está roto. Por
eso hay pruebas en las dos direcciones.

## Ataques de temporización

Una comparación de cadenas normal sale en cuanto encuentra el primer carácter
distinto. Eso significa que comparar un token que empieza bien tarda más que uno
que empieza mal.

La diferencia es de nanosegundos, pero es medible con suficientes intentos y
estadística. Un atacante puede adivinar el token carácter por carácter: prueba
todos los primeros caracteres, se queda con el que tardó más, y sigue.

**La defensa es comparar en tiempo constante:** una función que siempre recorre
la longitud completa sin salir antes. En Python es `hmac.compare_digest`.

Este proyecto la usa para el token del endpoint. Vale la pena notar que la
versión anterior comparaba hashes SHA-256, que en la práctica también es
constante porque ambos operandos miden lo mismo — pero `compare_digest` es la
primitiva que existe para esto y no obliga a razonar sobre por qué la otra
también servía.

## Datos personales y minimización

El principio de minimización dice: no recolectes lo que no necesitas, y no
guardes lo que no vas a usar.

Este proyecto lo aplica en dos lugares. El teléfono **no está** en la base de
conocimiento; hay una prueba automatizada que verifica, por patrón, que ningún
archivo del repositorio lo contenga. Y la telemetría registra veinticinco campos
por turno pero **nunca el texto**; también hay prueba.

La postura resultante es fuerte de decir: "no persisto texto de nadie, y aquí
está el test que lo enforza".

**La segunda barrera** es la redacción: aunque algo se colara, hay patrones que
lo sustituyen antes de emitir.

## URLs firmadas y el modelo de capacidades

Una URL firmada es una URL que lleva, en sus parámetros, una firma criptográfica
que autoriza una operación específica sobre un objeto específico durante un
tiempo limitado.

Es un ejemplo de **seguridad basada en capacidades**: en vez de verificar quién
eres cuando pides el recurso, se te entrega un token que *es* el permiso. Quien
tenga la URL puede acceder; quien no, no.

**Las propiedades que hay que poder enunciar.** Es de alcance limitado: solo ese
objeto, solo esa operación. Es temporal: caduca. Y es transferible, que es su
ventaja y su riesgo — puedes mandarla por correo, pero quien la reciba también
puede.

**El problema de firmar.** Firmar requiere una llave privada. Lo normal es
descargar el JSON de una cuenta de servicio, pero eso es una credencial de larga
vida en disco: si se filtra, hay que rotarla a mano.

**La alternativa en Google Cloud: firmar vía IAM.** La API de IAM tiene un método
`signBlob` que firma en nombre de una cuenta de servicio, usando el token
efímero que el entorno ya tiene. Cero llaves en disco.

## Identidades de carga de trabajo

El concepto general: en vez de darle credenciales a tu código, le das una
identidad al *entorno* donde corre, y los servicios reconocen esa identidad.

En Cloud Run, el servicio tiene una cuenta de servicio asociada. Cuando llama a
BigQuery o a Cloud Storage, esos servicios ven la identidad directamente. No hay
llave que rotar, ni que filtrar, ni que meter en el contenedor.

Este proyecto lo usa para todo lo de Google Cloud. La única credencial que sí es
un secreto — la API key de Anthropic, que es de un tercero — vive en Secret
Manager y Cloud Run la monta en el contenedor.

---

# Módulo 7 — Cloud Run y la infraestructura

## Contenedores

Un contenedor empaqueta una aplicación con todas sus dependencias en una imagen
que corre igual en cualquier lado. No es una máquina virtual: comparte el kernel
del anfitrión y aísla con mecanismos del sistema operativo — namespaces y
cgroups. Por eso arranca en segundos y no en minutos.

**Dos prácticas que este proyecto aplica y que un entrevistador nota.**

Correr como usuario no root. Si alguien logra ejecutar código dentro del
contenedor, root dentro del contenedor es un mejor punto de partida para escapar
que un usuario sin privilegios.

Fijar versiones exactas de dependencias. Con rangos, la imagen que construyes hoy
no es la que construiste ayer: un fallo que solo aparece en producción se vuelve
imposible de reproducir, y un despliegue de emergencia puede traer una versión
que nadie probó.

## Serverless y Cloud Run

Cloud Run corre contenedores sin que administres servidores. Escala según el
tráfico, incluso a cero.

**Cold start.** Si no hay instancias corriendo y llega una petición, hay que
arrancar un contenedor. En este proyecto son unos cuatro segundos. Por eso está
configurado con una instancia mínima: el evaluador nunca lo pega. Cuesta unos
dólares al mes.

**Concurrencia.** Cuántas peticiones simultáneas atiende una instancia. Este
proyecto está en cuarenta. El razonamiento: cada petición pasa la mayor parte del
tiempo esperando a la API del modelo, no calculando, así que una instancia puede
atender muchas esperas.

Si el trabajo fuera de CPU, la concurrencia alta sería contraproducente: las
peticiones competirían por el mismo procesador.

**Instancias máximas.** El tope de escalado, que en la práctica es el tope de
gasto. Sin él, un pico de tráfico es una factura.

## CPU throttling: el detalle que cuesta telemetría

Cloud Run tiene dos modelos de asignación de CPU.

**CPU solo durante peticiones**, que es el predeterminado y el más barato: entre
peticiones, la instancia tiene el CPU estrangulado casi a cero.

**CPU siempre asignado**: la instancia tiene CPU mientras existe.

**Por qué importa.** Si tu aplicación hace trabajo en segundo plano después de
responder — mandar telemetría, cerrar un lote, vaciar un buffer — con el modelo
predeterminado ese trabajo puede no completarse nunca, porque justo al terminar
la respuesta se acaba el CPU.

Y falla en silencio: la respuesta sale bien, el usuario está contento, y los
datos nunca llegan.

Este proyecto usa `--no-cpu-throttling` por eso.

## SIGTERM y apagado limpio

Cuando Cloud Run va a terminar una instancia — por escalado, por despliegue, por
reciclaje — manda una señal SIGTERM y da un margen antes de matar el proceso.

Una aplicación que ignora SIGTERM pierde lo que tenga en vuelo.

Este proyecto tiene una función que se llama en el ciclo de vida de la aplicación
y espera a que salgan las filas de telemetría pendientes. El razonamiento que
conviene poder decir: perder telemetría al apagar no es perder datos al azar. Se
pierde justo la del final — despliegues, picos de carga, reinicios por error —
que son los momentos sobre los que uno más quiere mirar los datos después.

## BigQuery: lo que hay que saber

Es un almacén de datos analítico, columnar y sin servidor. Columnar significa que
guarda los datos por columna y no por fila, lo cual hace que leer tres columnas
de una tabla de mil millones de filas sea barato.

**Particionado.** Dividir la tabla por un campo, típicamente una fecha. Una
consulta con filtro sobre ese campo solo lee las particiones que necesita. Sin
particionado, cada consulta escanea todo y paga por todo. La tabla de este
proyecto está particionada por día.

**Clustering.** Ordenar físicamente los datos dentro de cada partición por una o
más columnas. Acelera filtros y agregaciones sobre esas columnas. Esta tabla está
agrupada por conversación y modelo.

**Streaming inserts.** Insertar filas de a una, disponibles para consulta casi de
inmediato, en vez de cargar archivos por lotes. Es lo que usa la telemetría de
este proyecto.

**Evolución de esquema.** Se pueden agregar columnas nuevas a una tabla
existente, siempre que sean nullable. Lo que no se puede es cambiar el tipo de
una columna existente ni quitarla sin recrear.

Este proyecto tuvo un fallo instructivo aquí: el esquema estaba escrito dos veces
— en el código y a mano en un script — y al agregar columnas al código, el script
y la tabla se quedaron atrás. Las inserciones habrían empezado a fallar mientras
el servicio seguía respondiendo normal.

---

# Módulo 8 — Evaluación de sistemas con modelos de lenguaje

## Por qué las pruebas normales no bastan

Una prueba unitaria clásica afirma que una entrada produce una salida exacta. Con
un modelo de lenguaje eso no funciona: la misma pregunta produce respuestas
distintas, todas correctas.

Entonces hay que cambiar qué se afirma. Las opciones, de más barata a más cara:

**Afirmar sobre el proceso, no sobre el texto.** Qué herramienta se llamó, qué
identificadores se citaron, cuántas vueltas dio el loop. Es determinista y barato,
y en este proyecto es la columna vertebral del conjunto dorado.

**Afirmar presencia y ausencia de subcadenas.** Que la respuesta mencione
"BigQuery"; que no mencione un teléfono. Barato, pero frágil si se hace mal.

**Afirmar con expresiones regulares.** Útil para lo que no se puede escribir
literal en un repositorio público, como un patrón de teléfono.

**Usar un modelo como juez.** Un segundo modelo evalúa si la respuesta cumple un
criterio. Caro, no determinista, pero es lo único que captura matices.

## El conjunto dorado

Un conjunto de casos de prueba curado a mano, con su resultado esperado, que se
corre contra el sistema real. "Dorado" porque el resultado esperado se considera
la verdad de referencia.

**Las decisiones de diseño de uno bueno.** Que cubra las categorías que importan,
no solo las fáciles. Que incluya casos adversariales. Que afirme sobre el proceso
y no solo sobre el texto. Y que se pueda correr rápido y a mano, porque si cuesta
media hora nadie lo corre antes de desplegar.

**La pregunta que te van a hacer:** "pasan los cuarenta y cinco, ¿cómo sabes que
el conjunto es bueno y no que es fácil?" La respuesta honesta tiene dos partes.
Una: los casos salieron de fallos reales, no de imaginar qué podría fallar. Dos:
hay una forma de comprobarlo, que es introducir el bug a propósito y ver si la
prueba falla — y este proyecto lo hizo con todos los controles importantes.

## Pruebas frágiles

Una prueba frágil falla por razones que no son el fallo que busca. Su costo real
no es el falso positivo: es que entrena a quien la corre a ignorarla.

Este proyecto tuvo un ejemplo perfecto. Varios casos pedían que la respuesta
contuviera la subcadena "no " con espacio, como forma de verificar que el agente
negaba. Eso reprobó tres respuestas perfectas, porque "No. Kubernetes no aparece
en el CV" no contiene "no " con espacio: ahí el "No" viene con punto.

El espacio final era un límite de palabra escrito a mano. La lección: cuando una
prueba falla, la primera pregunta es si falló el código o falló la prueba.

**El error hermano: sobre-especificar.** Una aserción que fija el *mecanismo* en
vez del *resultado*. Un caso preguntaba "¿hace algo con IA fuera del trabajo?" y
exigía una herramienta específica. Pero había dos caminos válidos, y la aserción
reprobaba la mejor respuesta de las dos.

## Modelo como juez

Usar un modelo para evaluar la salida de otro. Es la técnica estándar para lo que
no se puede afirmar deterministamente.

**Los criterios típicos en RAG**, que vienen del framework Ragas y ya son
vocabulario común:

**Faithfulness**, o fidelidad: ¿cada afirmación de la respuesta está respaldada
por el contexto recuperado? Es la métrica anti-alucinación.

**Answer relevancy**: ¿la respuesta contesta lo que se preguntó?

**Context precision**: de lo recuperado, ¿qué fracción era relevante?

**Context recall**: de lo que hacía falta, ¿qué fracción se recuperó?

**Los riesgos del juez.** No es determinista, así que dos corridas pueden dar
distinto. Tiene sesgos conocidos — prefiere respuestas largas, prefiere
respuestas de modelos parecidos a él. Y cuesta dinero por evaluación.

La práctica sensata es calibrarlo: evaluar a mano una muestra, comparar con lo
que dice el juez, y ajustar el criterio hasta que coincidan razonablemente.

**En este proyecto el juez de fidelidad es un pendiente documentado**, y el
razonamiento de por qué importa es preciso: el conjunto dorado verifica que el
agente llamó la herramienta correcta y citó la entrada correcta, pero **no**
verifica que lo que dijo esté respaldado por lo que citó. Un modelo puede citar
bien e inventar alrededor.

## Pruebas offline y online

**Offline** son las que no llaman al modelo: recuperación, guardrails, protocolo,
parseo. Corren en segundos, cuestan cero, y van en integración continua en cada
push. Este proyecto tiene doscientas sesenta y tres.

**Online** son las que sí llaman al modelo. Cuestan dinero y tardan. Este
proyecto las corre a mano antes de desplegar.

**La trampa que este proyecto descubrió a la mala:** tener muchísimas pruebas
offline no garantiza cobertura de la ruta real. Un error trivial de alcance de
variable pasó limpio por ciento cincuenta y ocho pruebas offline y por cuarenta y
cinco de cuarenta y cinco del conjunto dorado — porque todas llamaban a la función
del agente directamente y ninguna cruzaba la capa HTTP. El agente estaba
perfecto; el servidor devolvía error quinientos en cada petición.

La lección: la suite medía la parte difícil y no miraba la fácil.

## Probar que la prueba funciona

La disciplina más útil de todo el proyecto, y la más fácil de saltarse.

Una prueba que nunca se ha visto fallar no prueba nada. Puede estar afirmando
algo trivialmente cierto, puede tener un error que la hace pasar siempre, puede
estar probando la función equivocada.

**El método:** reintroduce el bug a propósito, corre la prueba, confirma que
falla, revierte.

En este proyecto se hizo con todos los controles importantes, y en un caso salvó
de un falso sentido de seguridad: la primera versión de la prueba del redactor de
flujo revisaba cada delta por separado y **pasaba con el bug puesto**, porque un
teléfono partido no dispara el patrón en ningún fragmento suelto. Había que
revisar el texto ensamblado, que es lo que ve el cliente.

---

# Cierre — cómo razonar una pregunta que no te sabes

Si en la entrevista aparece algo que no cubriste, hay una estructura que casi
siempre funciona, y es la misma que usa este proyecto para tomar decisiones.

**Primero, nombra el compromiso.** Casi toda pregunta de diseño es un intercambio
entre dos cosas que no se pueden maximizar a la vez: latencia contra calidad,
costo contra cobertura, seguridad contra usabilidad, simplicidad contra
generalidad. Decir cuál es el compromiso ya demuestra que entiendes el problema.

**Segundo, di qué elegirías y por qué, en términos de la carga real.** No "X es
mejor que Y", sino "para esta carga, donde el cuello de botella es Z, elegiría X".

**Tercero, di qué te haría cambiar de opinión, y cómo lo medirías.** Esta es la
parte que separa una opinión de una decisión de ingeniería. Si puedes nombrar el
número que te haría cambiar, demuestras que la decisión es reversible y está
fundamentada.

**Y cuarto, si no lo sabes, dilo.** "No lo he medido" y "eso no lo verifiqué" son
respuestas fuertes cuando van acompañadas de cómo lo averiguarías. Un
entrevistador senior detecta cuando alguien defiende algo que no comprobó, y
detectarlo cuesta más credibilidad que la pregunta original.
