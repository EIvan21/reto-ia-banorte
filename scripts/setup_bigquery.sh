#!/usr/bin/env bash
# Crea el dataset y la tabla donde el agente escribe su telemetria.
#
# La tabla queda particionada por dia y agrupada por categoria de evento, que es
# como la consulta el Agent Analytics Block de looker-open-source.
#
#   ./scripts/setup_bigquery.sh

set -euo pipefail

PROYECTO="${BQ_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
DATASET="${BQ_DATASET:-cv_agent}"
TABLA="${BQ_TABLE:-agent_events}"
UBICACION="${BQ_LOCATION:-US}"

if [[ -z "$PROYECTO" ]]; then
  echo "ERROR: no hay proyecto. Corre: gcloud config set project TU_PROYECTO" >&2
  exit 1
fi

echo "==> Proyecto: $PROYECTO"
echo "==> Dataset:  $DATASET"
echo "==> Tabla:    $TABLA"
echo

gcloud services enable bigquery.googleapis.com --project "$PROYECTO" --quiet

if bq --project_id="$PROYECTO" show --dataset "$DATASET" &>/dev/null; then
  echo "==> El dataset ya existe."
else
  echo "==> Creando dataset..."
  bq --project_id="$PROYECTO" --location="$UBICACION" mk --dataset \
    --description "Telemetria del agente de CV" "$PROYECTO:$DATASET"
fi

# El esquema se DERIVA de app/telemetry.py, no se escribe aqui. Estuvo
# duplicado en un heredoc y paso lo que siempre pasa: se agregaron cuatro
# columnas al codigo y este script se quedo con las viejas, asi que la tabla
# habria rechazado las inserciones. Una segunda fuente de verdad no se
# desincroniza si tienes cuidado; se desincroniza y punto.
ESQUEMA="$(mktemp)"
PY_BIN="${PY_BIN:-python}"
"$PY_BIN" - <<'PYEOF' > "$ESQUEMA"
import json, os, sys
sys.path.insert(0, os.getcwd())
from app.telemetry import ESQUEMA_BQ
# BigQuery solo admite AGREGAR columnas nullable a una tabla que ya existe.
print(json.dumps([{**c, "mode": c.get("mode", "NULLABLE")} for c in ESQUEMA_BQ], indent=2))
PYEOF

if ! [ -s "$ESQUEMA" ]; then
  echo "ERROR: no se pudo generar el esquema desde app/telemetry.py." >&2
  echo "       Corre este script desde la raiz del repo, con el venv activo," >&2
  echo "       o pasa PY_BIN=./.venv/bin/python (o .venv/Scripts/python.exe)." >&2
  rm -f "$ESQUEMA"
  exit 1
fi

echo "==> Esquema derivado de app/telemetry.py: $(grep -c '"name"' "$ESQUEMA") columnas"

if bq --project_id="$PROYECTO" show "$DATASET.$TABLA" &>/dev/null; then
  echo "==> La tabla ya existe; se actualiza el esquema si hay campos nuevos."
  bq --project_id="$PROYECTO" update "$DATASET.$TABLA" "$ESQUEMA"
else
  echo "==> Creando tabla particionada por dia..."
  bq --project_id="$PROYECTO" mk --table \
    --time_partitioning_field marca_tiempo \
    --time_partitioning_type DAY \
    --clustering_fields id_conversacion,modelo \
    --description "Un renglon por turno de conversacion del agente de CV" \
    "$PROYECTO:$DATASET.$TABLA" "$ESQUEMA"
fi

rm -f "$ESQUEMA"

echo
echo "==> Listo. Para activar el sink, despliega con:"
echo "      BQ_PROJECT=$PROYECTO BQ_DATASET=$DATASET ./scripts/deploy.sh"
echo
echo "==> Consultas utiles una vez que haya trafico:"
cat <<SQL

  -- Salud del agente en las ultimas 24 h
  SELECT
    COUNTIF(exitoso) / COUNT(*)      AS tasa_exito,
    COUNTIF(fundamentado) / COUNT(*) AS tasa_fundamentacion,
    APPROX_QUANTILES(latencia_ms, 100)[OFFSET(50)] AS p50_ms,
    APPROX_QUANTILES(latencia_ms, 100)[OFFSET(95)] AS p95_ms,
    SUM(tokens_entrada + tokens_salida) AS tokens
  FROM \`$PROYECTO.$DATASET.$TABLA\`
  WHERE marca_tiempo > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 24 HOUR);

  -- De que le preguntan al agente. Es la consulta que responde la pregunta que
  -- un dueno de agente realmente quiere contestar.
  SELECT categoria, COUNT(*) AS turnos,
         ROUND(AVG(latencia_ms)) AS latencia_media_ms,
         ROUND(COUNTIF(fundamentado) / COUNT(*), 2) AS tasa_fundamentacion
  FROM \`$PROYECTO.$DATASET.$TABLA\`
  GROUP BY categoria ORDER BY turnos DESC;

  -- Que herramientas se usan mas (revela que le interesa a quien pregunta)
  SELECT herramienta, COUNT(*) AS veces
  FROM \`$PROYECTO.$DATASET.$TABLA\`, UNNEST(herramientas_usadas) AS herramienta
  GROUP BY herramienta ORDER BY veces DESC;

  -- Turnos que dispararon un guardrail
  SELECT marca_tiempo, id_conversacion, etiquetas_guardrail
  FROM \`$PROYECTO.$DATASET.$TABLA\`
  WHERE ARRAY_LENGTH(etiquetas_guardrail) > 0
  ORDER BY marca_tiempo DESC LIMIT 50;

SQL
