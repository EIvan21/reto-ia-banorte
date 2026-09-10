#!/usr/bin/env bash
# Despliega el agente en Cloud Run.
#
# La API key nunca se pasa como variable de entorno en claro: vive en Secret
# Manager y Cloud Run la monta en el contenedor. Asi la clave no aparece ni en
# el historial de shell, ni en la configuracion del servicio, ni en los logs.
#
#   ./scripts/deploy.sh
#
# Requiere: gcloud autenticado y un proyecto con facturacion activa.

set -euo pipefail

# El proyecto de este agente, fijado en el repo. Antes se heredaba de
# `gcloud config get-value project`, es decir del estado global de la maquina:
# con otro proyecto activo, este script creo secretos con la API key, un bucket
# y permisos IAM en el proyecto equivocado antes de fallar el build. Un
# despliegue no debe depender de en que directorio estuvo uno antes.
#
# Se puede apuntar a otro proyecto a proposito con PROYECTO=... ./deploy.sh
PROYECTO_ESPERADO="cv-agent-edher"
PROYECTO="${PROYECTO:-$PROYECTO_ESPERADO}"
REGION="${REGION:-us-central1}"
SERVICIO="${SERVICIO:-cv-agent}"
SECRETO="${SECRETO:-anthropic-api-key}"

# Instancias minimas. En 1, el evaluador nunca pega con un arranque en frio
# (que en este contenedor son ~4 s). Cuesta unos pocos dolares al mes, asi que
# conviene bajarlo a 0 cuando termine la evaluacion.
MIN_INSTANCIAS="${MIN_INSTANCIAS:-1}"
MAX_INSTANCIAS="${MAX_INSTANCIAS:-10}"

if [[ -z "$PROYECTO" ]]; then
  echo "ERROR: no hay proyecto de GCP." >&2
  exit 1
fi

ACTIVO="$(gcloud config get-value project 2>/dev/null || true)"
if [[ -n "$ACTIVO" && "$ACTIVO" != "$PROYECTO" ]]; then
  echo "AVISO: el proyecto activo de gcloud es '$ACTIVO', y se va a desplegar" >&2
  echo "       en '$PROYECTO'. Se usa el del script, no el del ambiente." >&2
fi

echo "==> Proyecto:  $PROYECTO"
echo "==> Region:    $REGION"
echo "==> Servicio:  $SERVICIO"
echo

echo "==> Habilitando APIs necesarias..."
gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  --project "$PROYECTO" --quiet

# --- Secreto con la API key de Anthropic ------------------------------------
if ! gcloud secrets describe "$SECRETO" --project "$PROYECTO" &>/dev/null; then
  echo
  echo "==> El secreto '$SECRETO' no existe. Se va a crear."

  # Si hay un .env local con la clave, se usa esa. Evita volver a teclearla y
  # permite que el despliegue corra sin intervencion.
  CLAVE=""
  if [[ -f .env ]] && grep -q '^ANTHROPIC_API_KEY=' .env; then
    CLAVE="$(grep '^ANTHROPIC_API_KEY=' .env | head -1 | cut -d= -f2- | tr -d '\r\n"'"'"' ')"
    echo "    Tomada del archivo .env local."
  fi

  if [[ -z "$CLAVE" ]]; then
    echo "    Pega tu API key de Anthropic y presiona Enter."
    echo "    (No se muestra en pantalla y no queda en el historial del shell.)"
    read -rs CLAVE
    echo
  fi

  if [[ -z "$CLAVE" ]]; then
    echo "ERROR: no hay API key. Crea .env con ANTHROPIC_API_KEY=... o tecleala." >&2
    exit 1
  fi

  printf '%s' "$CLAVE" | gcloud secrets create "$SECRETO" \
    --data-file=- --replication-policy=automatic --project "$PROYECTO"
  unset CLAVE
  echo "==> Secreto creado."
else
  echo "==> El secreto '$SECRETO' ya existe; se reutiliza."
fi

# --- Bearer token del propio endpoint ---------------------------------------
# Sin esto, cualquiera que descubra la URL puede gastar la API key de Anthropic.
# Se genera una sola vez y se guarda en Secret Manager; se registra en el campo
# "Clave de API" de la plataforma del reto.
SECRETO_AGENTE="${SECRETO_AGENTE:-agent-api-key}"
if ! gcloud secrets describe "$SECRETO_AGENTE" --project "$PROYECTO" &>/dev/null; then
  echo "==> Generando token de acceso al endpoint..."
  python -c "import secrets; print(secrets.token_urlsafe(32), end='')" \
    | gcloud secrets create "$SECRETO_AGENTE" \
      --data-file=- --replication-policy=automatic --project "$PROYECTO"
else
  echo "==> El token del endpoint ya existe; se reutiliza."
fi

# La cuenta de servicio por defecto de Cloud Run necesita leer ambos secretos.
NUMERO_PROYECTO="$(gcloud projects describe "$PROYECTO" --format='value(projectNumber)')"
CUENTA_SERVICIO="${NUMERO_PROYECTO}-compute@developer.gserviceaccount.com"

for s in "$SECRETO" "$SECRETO_AGENTE"; do
  echo "==> Dando acceso a '$s' a ${CUENTA_SERVICIO}..."
  gcloud secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:${CUENTA_SERVICIO}" \
    --role="roles/secretmanager.secretAccessor" \
    --project "$PROYECTO" --quiet >/dev/null
done

# Permisos para que Cloud Build construya la imagen.
#
# En proyectos creados despues del cambio de 2024, la cuenta de servicio por
# defecto YA NO recibe estos roles automaticamente, y el despliegue falla con un
# PERMISSION_DENIED al leer el codigo fuente que el propio gcloud acaba de subir.
# El mensaje no dice que rol falta, asi que se otorgan explicitamente aqui: es la
# diferencia entre un script reproducible y uno que solo funciona en la maquina
# donde ya se configuro a mano.
echo "==> Otorgando permisos de build a ${CUENTA_SERVICIO}..."
for rol in \
  roles/cloudbuild.builds.builder \
  roles/storage.objectViewer \
  roles/artifactregistry.writer \
  roles/logging.logWriter
do
  gcloud projects add-iam-policy-binding "$PROYECTO" \
    --member="serviceAccount:${CUENTA_SERVICIO}" \
    --role="$rol" --quiet >/dev/null
done

# Escritura en BigQuery, solo si hay telemetria configurada. Sin estos roles el
# agente responde bien pero la telemetria falla en silencio en el hilo de fondo:
# el sink es best-effort a proposito, asi que el fallo se anota en stdout y nadie
# lo mira. Se otorgan aqui para que ese caso no exista.
if [[ -n "${BQ_PROJECT:-}" && -n "${BQ_DATASET:-}" ]]; then
  echo "==> Otorgando escritura en BigQuery a ${CUENTA_SERVICIO}..."
  for rol in roles/bigquery.dataEditor roles/bigquery.jobUser; do
    gcloud projects add-iam-policy-binding "$PROYECTO" \
      --member="serviceAccount:${CUENTA_SERVICIO}" \
      --role="$rol" --quiet >/dev/null
  done
fi

# --- Bucket para reportes descargables --------------------------------------
# Los reportes se sirven con URL firmada, no publica: pueden contener el analisis
# de una vacante concreta y no tienen por que ser enumerables ni indexables.
#
# Firmar desde Cloud Run con la cuenta por defecto exige que esa cuenta pueda
# firmar EN SU PROPIO NOMBRE (serviceAccountTokenCreator sobre si misma). Sin ese
# rol la subida funciona y la firma truena, asi que el fallo aparece al final,
# cuando el archivo ya existe.
BUCKET_REPORTES="${BUCKET_REPORTES:-${PROYECTO}-reportes}"
if ! gcloud storage buckets describe "gs://${BUCKET_REPORTES}" --project "$PROYECTO" &>/dev/null; then
  echo "==> Creando bucket de reportes gs://${BUCKET_REPORTES}..."
  gcloud storage buckets create "gs://${BUCKET_REPORTES}" \
    --project "$PROYECTO" --location "$REGION" \
    --uniform-bucket-level-access --quiet
else
  echo "==> El bucket de reportes ya existe; se reutiliza."
fi

echo "==> Permisos de escritura y firma para ${CUENTA_SERVICIO}..."
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET_REPORTES}" \
  --member="serviceAccount:${CUENTA_SERVICIO}" \
  --role="roles/storage.objectAdmin" --project "$PROYECTO" --quiet >/dev/null
gcloud iam service-accounts add-iam-policy-binding "$CUENTA_SERVICIO" \
  --member="serviceAccount:${CUENTA_SERVICIO}" \
  --role="roles/iam.serviceAccountTokenCreator" --project "$PROYECTO" --quiet >/dev/null

# --- Despliegue -------------------------------------------------------------
# Primer despliegue sin PUBLIC_BASE_URL: todavia no se conoce la URL. Se corrige
# en el segundo paso, una vez que Cloud Run la asigna.
echo
# --- Sello de version -------------------------------------------------------
# `gcloud run deploy --source .` sube la CARPETA LOCAL, no lo que esta en
# GitHub. Sin dejar constancia del commit, produccion podia traer codigo que no
# existe en ningun otro lado y nadie tenia como notarlo: las revisiones de Cloud
# Run no guardan referencia a git.
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo desconocido)"
# Numero de parche de la version. Sube solo en cada commit, siempre hacia
# arriba, y deja comparar dos despliegues de un vistazo.
GIT_BUILD="$(git rev-list --count HEAD 2>/dev/null || echo 0)"
if [[ -n "$(git status --porcelain 2>/dev/null)" ]]; then
  GIT_LIMPIO="false"
  echo "AVISO: hay cambios sin commitear y se van a desplegar." >&2
  echo "       El servicio quedara marcado como construido desde un arbol sucio." >&2
  git status --short >&2
else
  GIT_LIMPIO="true"
fi
echo "==> Version:   1.1.$GIT_BUILD  ($GIT_SHA, arbol limpio: $GIT_LIMPIO)"

# El nombre de la revision lleva el commit, para que cada despliegue se pueda
# rastrear hasta su mensaje en GitHub. Por defecto Cloud Run las llama
# cv-agent-00021-7sr: dicen cuando se desplego, no que cambio.
SUFIJO="$GIT_SHA"
if gcloud run revisions describe "${SERVICIO}-${SUFIJO}" \
     --region "$REGION" --project "$PROYECTO" &>/dev/null; then
  # Ya se desplego este commit antes. Se distingue por hora en vez de fallar.
  SUFIJO="${GIT_SHA}-$(date +%H%M%S)"
fi
echo "==> Revision:  ${SERVICIO}-${SUFIJO}"


# La URL de Cloud Run es estable una vez creado el servicio, asi que en todo
# despliegue que no sea el primero ya se conoce de antemano. Averiguarla aqui
# permite mandar PUBLIC_BASE_URL en el mismo despliegue, en vez de desplegar y
# luego actualizar: eso creaba DOS revisiones por cada despliegue y dejaba el
# historial ilegible.
URL="$(gcloud run services describe "$SERVICIO" \
  --project "$PROYECTO" --region "$REGION" --format='value(status.url)' 2>/dev/null || true)"

VARS_URL=""
if [[ -n "$URL" ]]; then
  VARS_URL=",PUBLIC_BASE_URL=${URL}"
  echo "==> URL:       $URL (ya conocida)"
fi

echo "==> Desplegando (build remoto con Cloud Build)..."
gcloud run deploy "$SERVICIO" \
  --source . \
  --revision-suffix "$SUFIJO" \
  --project "$PROYECTO" \
  --region "$REGION" \
  --platform managed \
  --allow-unauthenticated \
  --port 8080 \
  --memory 512Mi \
  --cpu 1 \
  --timeout 300 \
  --concurrency 40 \
  --min-instances "$MIN_INSTANCIAS" \
  --max-instances "$MAX_INSTANCIAS" \
  --set-secrets "ANTHROPIC_API_KEY=${SECRETO}:latest,AGENT_API_KEY=${SECRETO_AGENTE}:latest" \
  --set-env-vars "MODEL=claude-opus-5,EFFORT=low,BQ_PROJECT=${BQ_PROJECT:-},BQ_DATASET=${BQ_DATASET:-},REPORTES_BUCKET=${BUCKET_REPORTES},GIT_SHA=${GIT_SHA},GIT_BUILD=${GIT_BUILD},GIT_LIMPIO=${GIT_LIMPIO}${VARS_URL}" \
  --quiet

if [[ -z "$URL" ]]; then
  # Primer despliegue: el servicio no existia, asi que la URL nace aqui. La
  # tarjeta de agente la publica y el transporte MCP valida contra ella, asi
  # que este segundo paso no es opcional -- pero solo ocurre esta vez.
  URL="$(gcloud run services describe "$SERVICIO" \
    --project "$PROYECTO" --region "$REGION" --format='value(status.url)')"
  echo
  echo "==> Primer despliegue: fijando PUBLIC_BASE_URL=${URL}"
  gcloud run services update "$SERVICIO" \
    --project "$PROYECTO" --region "$REGION" \
    --update-env-vars "PUBLIC_BASE_URL=${URL}" --quiet >/dev/null
fi

TOKEN="$(gcloud secrets versions access latest --secret="$SECRETO_AGENTE" --project "$PROYECTO")"

echo
echo "======================================================================"
echo "  Desplegado"
echo "======================================================================"
echo "  Registro en la plataforma del reto"
echo "    Boton 'Importar desde tarjeta de agente':"
echo "        ${URL}"
echo "    O a mano -- URL base:"
echo "        ${URL}/v1"
echo "    Estado de la conversacion:"
echo "        Reproducir transcripcion (sin estado)"
echo "    Clave de API:"
echo "        ${TOKEN}"
echo
echo "  Reportes descargables:"
echo "        gs://${BUCKET_REPORTES}"
echo
echo "  Servidor MCP (para cualquier otro agente):"
echo "        ${URL}/mcp"
echo
echo "  Verificacion:"
echo "        curl ${URL}/salud"
echo "        curl -X POST ${URL}/v1/responses \\"
echo "          -H 'Authorization: Bearer ${TOKEN}' \\"
echo "          -H 'Content-Type: application/json' \\"
echo "          -d '{\"input\":\"¿Que experiencia tiene con LLMs?\"}'"
echo "======================================================================"
