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

ESQUEMA="$(mktemp)"
cat > "$ESQUEMA" <<'JSON'
[
  {"name":"id_evento","type":"STRING","mode":"REQUIRED"},
  {"name":"marca_tiempo","type":"TIMESTAMP","mode":"REQUIRED"},
  {"name":"id_respuesta","type":"STRING"},
  {"name":"id_conversacion","type":"STRING"},
  {"name":"modelo","type":"STRING"},
  {"name":"latencia_ms","type":"INTEGER"},
  {"name":"tokens_entrada","type":"INTEGER"},
  {"name":"tokens_salida","type":"INTEGER"},
  {"name":"herramientas_usadas","type":"STRING","mode":"REPEATED"},
  {"name":"num_herramientas","type":"INTEGER"},
  {"name":"citas","type":"STRING","mode":"REPEATED"},
  {"name":"num_citas","type":"INTEGER"},
  {"name":"fundamentado","type":"BOOLEAN"},
  {"name":"etiquetas_guardrail","type":"STRING","mode":"REPEATED"},
  {"name":"turnos_herramienta","type":"INTEGER"},
  {"name":"streaming","type":"BOOLEAN"},
  {"name":"error","type":"STRING"},
  {"name":"exitoso","type":"BOOLEAN"}
]
JSON

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
