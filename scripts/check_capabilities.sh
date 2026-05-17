#!/usr/bin/env bash
# Read-only tier/capability probe. No writes, nothing to clean up.
#
# Critical question: is the Transformations API available on this account
# tier? The entire v1 remediation path (deploy VIEW shim via
# POST /v1/transformations) depends on it. Also inventories existing
# connections and destinations so we know what's already wired.
#
# Credentials are read from deploy/.env (or the existing environment).

set -u

API="https://api.fivetran.com"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$ROOT/deploy/.env"

if [[ -z "${FIVETRAN_API_KEY:-}" || -z "${FIVETRAN_API_SECRET:-}" ]]; then
  if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
  fi
fi

if [[ -z "${FIVETRAN_API_KEY:-}" || -z "${FIVETRAN_API_SECRET:-}" ]]; then
  echo "ERROR: FIVETRAN_API_KEY / FIVETRAN_API_SECRET not set."
  echo "       Fill $ENV_FILE (see deploy/env.example) or export them."
  exit 2
fi

AUTH="$FIVETRAN_API_KEY:$FIVETRAN_API_SECRET"
body="$(mktemp)"
trap 'rm -f "$body"' EXIT

# probe <label> <path> <critical:yes|no>
probe() {
  local label="$1" path="$2" critical="$3"
  local code
  code="$(curl -s -o "$body" -w '%{http_code}' -u "$AUTH" "$API$path")"
  if [[ "$code" == "200" ]]; then
    local count
    count="$(python3 -c '
import json,sys
try:
    d=json.load(open(sys.argv[1])).get("data",{})
    items=d.get("items", d if isinstance(d,list) else [])
    print(len(items))
except Exception:
    print("?")' "$body" 2>/dev/null)"
    echo "AVAILABLE  : $label (HTTP 200, items=$count)"
    return 0
  fi
  if [[ "$critical" == "yes" ]]; then
    echo "GATED?     : $label -> HTTP $code  *** CRITICAL ***"
  else
    echo "UNAVAILABLE: $label -> HTTP $code"
  fi
  return 1
}

echo "== Transformations API (CRITICAL for v1 remediation) =="
tp_ok=0; tr_ok=0
probe "transformation-projects (GET /v1/transformation-projects)" "/v1/transformation-projects" yes && tp_ok=1
probe "transformations       (GET /v1/transformations)"          "/v1/transformations"          yes && tr_ok=1

echo
echo "== Inventory =="
probe "connections (GET /v1/connections)"   "/v1/connections"   no || true
# show connection ids/services if any
curl -s -u "$AUTH" "$API/v1/connections" | python3 -c '
import json,sys
try:
    items=json.load(sys.stdin).get("data",{}).get("items",[])
    for c in items:
        print("             - %-24s %s  schema=%s" % (
            c.get("id",""), c.get("service",""), c.get("schema","")))
    if not items: print("             (none yet)")
except Exception:
    print("             (could not parse connections)")'

probe "destinations (GET /v1/destinations)" "/v1/destinations" no || true
curl -s -u "$AUTH" "$API/v1/destinations" | python3 -c '
import json,sys
try:
    items=json.load(sys.stdin).get("data",{}).get("items",[])
    for d in items:
        print("             - %-24s %s  group=%s" % (
            d.get("id",""), d.get("service",""), d.get("group_id","")))
    if not items: print("             (none yet)")
except Exception:
    print("             (could not parse destinations)")'

echo
if [[ "$tp_ok" == "1" && "$tr_ok" == "1" ]]; then
  echo "VERDICT: Transformations API is AVAILABLE. v1 remediation path is viable."
else
  echo "VERDICT: Transformations API appears GATED on this tier."
  echo "         v1 remediation must fall back to modify_connection_column_config"
  echo "         or generating SQL for the user to run manually. Decide before"
  echo "         investing in source DB + connector setup."
  exit 1
fi
