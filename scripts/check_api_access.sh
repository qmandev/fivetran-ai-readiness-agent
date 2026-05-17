#!/usr/bin/env bash
# Verify the Fivetran API key: confirms READ auth, then probes WRITE role
# with a fully reversible, self-cleaning inactive-webhook create + delete.
#
# Credentials are read from deploy/.env (or the existing environment) so the
# secret never lands in shell history. Nothing is left behind in Fivetran.

set -u

API="https://api.fivetran.com"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$ROOT/deploy/.env"

# Load deploy/.env unless the vars are already exported.
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

# ---- 1. READ test -----------------------------------------------------------
code="$(curl -s -o "$body" -w '%{http_code}' -u "$AUTH" "$API/v1/account/info")"
if [[ "$code" == "200" ]]; then
  echo "READ-OK    : /v1/account/info returned 200"
elif [[ "$code" == "401" ]]; then
  echo "READ-FAIL  : 401 Unauthorized — key/secret invalid. Stopping."
  exit 1
else
  echo "READ-FAIL  : unexpected HTTP $code from /v1/account/info. Stopping."
  cat "$body"; echo
  exit 1
fi

# ---- 2. WRITE probe (reversible) -------------------------------------------
# active:false makes Fivetran skip the URL-reachability test, so the dummy
# URL is never contacted.
code="$(curl -s -o "$body" -w '%{http_code}' -u "$AUTH" \
  -X POST "$API/v1/webhooks/account" \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/unused","events":["sync_end"],"active":false}')"

case "$code" in
  200|201)
    wid="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("data",{}).get("id",""))' "$body" 2>/dev/null)"
    echo "WRITE-OK   : created probe webhook (HTTP $code)${wid:+, id=$wid}"
    if [[ -n "$wid" ]]; then
      dcode="$(curl -s -o /dev/null -w '%{http_code}' -u "$AUTH" \
        -X DELETE "$API/v1/webhooks/$wid")"
      if [[ "$dcode" == "200" ]]; then
        echo "CLEANUP-OK : probe webhook $wid deleted"
      else
        echo "CLEANUP-WARN: could not delete webhook $wid (HTTP $dcode) — remove it manually"
      fi
    else
      echo "CLEANUP-WARN: created a webhook but could not parse its id — check the dashboard"
    fi
    echo
    echo "VERDICT: key has READ + WRITE. Agent write paths are usable."
    ;;
  401|403)
    echo "WRITE-DENIED: HTTP $code on create-webhook — key is read-only."
    echo
    echo "VERDICT: READ works, WRITE blocked. Upgrade the owning user's"
    echo "         account role (e.g., Account Administrator) before wiring"
    echo "         webhook registration / transformations."
    exit 1
    ;;
  *)
    echo "WRITE-UNKNOWN: unexpected HTTP $code on create-webhook:"
    cat "$body"; echo
    exit 1
    ;;
esac
