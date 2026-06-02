#!/usr/bin/env bash
# setup_external_logging.sh — one-time Fivetran external-logging configuration.
#
# Configures Fivetran to pipe sync failure events into the agent_state.sync_failure_log
# BigQuery table so that diagnose_sync_failures() has data to work with.
#
# Fivetran's external-logging API (POST /v1/external-logging) accepts a destination
# config.  We route to BigQuery using the same service account and dataset the agent
# already uses for its state tables.
#
# Prerequisites:
#   - FIVETRAN_API_KEY and FIVETRAN_API_SECRET set in the environment (or deploy/.env)
#   - GCP_PROJECT_ID set (or GOOGLE_CLOUD_PROJECT)
#   - The agent_state dataset and 07_sync_failure_log.sql DDL applied
#   - The Fivetran service account has BigQuery Data Editor + Job User on the project
#
# Usage:
#   source deploy/.env        # load credentials
#   bash scripts/setup_external_logging.sh
#
# This is idempotent — re-running updates the existing config if one exists.

set -euo pipefail

API_KEY="${FIVETRAN_API_KEY:?FIVETRAN_API_KEY not set}"
API_SECRET="${FIVETRAN_API_SECRET:?FIVETRAN_API_SECRET not set}"
PROJECT="${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:?GCP_PROJECT_ID not set}}"
STATE_DATASET="${BQ_STATE_DATASET:-agent_state}"
BQ_LOCATION="${BQ_LOCATION:-us-east1}"

TOKEN=$(printf "%s:%s" "$API_KEY" "$API_SECRET" | base64)

echo "→ Configuring Fivetran external logging → BigQuery"
echo "  Project:  $PROJECT"
echo "  Dataset:  $STATE_DATASET"
echo "  Location: $BQ_LOCATION"

RESPONSE=$(curl -s -w "\n%{http_code}" \
  -X POST "https://api.fivetran.com/v1/external-logging" \
  -H "Authorization: Basic $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{
    \"service\": \"big_query\",
    \"enabled\": true,
    \"config\": {
      \"project_id\": \"$PROJECT\",
      \"dataset_id\": \"$STATE_DATASET\",
      \"location\": \"$BQ_LOCATION\"
    }
  }")

HTTP_BODY=$(echo "$RESPONSE" | head -n -1)
HTTP_CODE=$(echo "$RESPONSE" | tail -n 1)

if [[ "$HTTP_CODE" == "200" || "$HTTP_CODE" == "201" ]]; then
  echo "✅ External logging configured (HTTP $HTTP_CODE)"
  echo "$HTTP_BODY" | python3 -c "import json,sys; d=json.load(sys.stdin); print('  Log service ID:', d.get('data',{}).get('id','(unknown)'))" 2>/dev/null || true
else
  echo "❌ External logging setup failed (HTTP $HTTP_CODE)"
  echo "$HTTP_BODY"
  exit 1
fi

echo ""
echo "Failure events will begin appearing in:"
echo "  \`$PROJECT.$STATE_DATASET.sync_failure_log\`"
echo ""
echo "Test with:"
echo "  bq query --location=$BQ_LOCATION --use_legacy_sql=false \\"
echo "    'SELECT * FROM \`$PROJECT.$STATE_DATASET.sync_failure_log\` LIMIT 5'"
