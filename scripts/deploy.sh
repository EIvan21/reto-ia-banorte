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

PROYECTO="${PROYECTO:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${REGION:-us-central1}"
SERVICIO="${SERVICIO:-cv-agent}"
SECRETO="${SECRETO:-anthropic-api-key}"

# Instancias minimas. En 1, el evaluador nunca pega con un arranque en frio
# (que en este contenedor son ~4 s). Cuesta unos pocos dolares al mes, asi que
# conviene bajarlo a 0 cuando termine la evaluacion.
MIN_INSTANCIAS="${MIN_INSTANCIAS:-1}"
MAX_INSTANCIAS="${MAX_INSTANCIAS:-10}"

if [[ -z "$PROYECTO" ]]; then
  echo "ERROR: no hay proyecto de GCP. Corre: gcloud config set project TU_PROYECTO" >&2
  exit 1
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
  echo "    Pega tu API key de Anthropic y presiona Enter."
  echo "    (No se muestra en pantalla y no queda en el historial del shell.)"
  read -rs CLAVE
  echo
  printf '%s' "$CLAVE" | gcloud secrets create "$SECRETO" \
    --data-file=- --replication-policy=automatic --project "$PROYECTO"
  unset CLAVE
  echo "==> Secreto creado."
else
  echo "==> El secreto '$SECRETO' ya existe; se reutiliza."
fi

# La cuenta de servicio por defecto de Cloud Run necesita poder leer el secreto.
NUMERO_PROYECTO="$(gcloud projects describe "$PROYECTO" --format='value(projectNumber)')"
CUENTA_SERVICIO="${NUMERO_PROYECTO}-compute@developer.gserviceaccount.com"

echo "==> Dando acceso al secreto a ${CUENTA_SERVICIO}..."
gcloud secrets add-iam-policy-binding "$SECRETO" \
  --member="serviceAccount:${CUENTA_SERVICIO}" \
  --role="roles/secretmanager.secretAccessor" \
  --project "$PROYECTO" --quiet >/dev/null

# --- Despliegue -------------------------------------------------------------
# Primer despliegue sin PUBLIC_BASE_URL: todavia no se conoce la URL. Se corrige
# en el segundo paso, una vez que Cloud Run la asigna.
echo
echo "==> Desplegando (build remoto con Cloud Build)..."
gcloud run deploy "$SERVICIO" \
  --source . \
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
  --set-secrets "ANTHROPIC_API_KEY=${SECRETO}:latest" \
  --set-env-vars "MODEL=claude-opus-5,EFFORT=low,BQ_PROJECT=${BQ_PROJECT:-},BQ_DATASET=${BQ_DATASET:-}" \
  --quiet

URL="$(gcloud run services describe "$SERVICIO" \
  --project "$PROYECTO" --region "$REGION" --format='value(status.url)')"

# La tarjeta de agente publica esta URL, asi que tiene que ser la real.
echo
echo "==> Fijando PUBLIC_BASE_URL=${URL}"
gcloud run services update "$SERVICIO" \
  --project "$PROYECTO" --region "$REGION" \
  --update-env-vars "PUBLIC_BASE_URL=${URL}" --quiet >/dev/null

echo
echo "======================================================================"
echo "  Desplegado"
echo "======================================================================"
echo "  URL base a registrar en la plataforma:"
echo "      ${URL}/v1"
echo
echo "  Tarjeta de agente (boton 'Importar desde tarjeta de agente'):"
echo "      ${URL}"
echo
echo "  Verificacion rapida:"
echo "      curl ${URL}/healthz"
echo "======================================================================"
