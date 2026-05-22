#!/usr/bin/env bash
# Register a Fivetran sync_end webhook pointing at a webhook.site URL — for
# checklist G4 (live sync_end payload capture). Inspect the captured payload
# to verify shape vs the documented sample in the Fivetran REST API docs.
#
# WORKFLOW:
#   1. Open https://webhook.site in a browser, copy your unique URL.
#   2. Run:   bash scripts/capture_webhook_payload.sh https://webhook.site/<uuid>
#      (this creates an ACCOUNT-level webhook for `sync_end`, active)
#   3. Trigger a Fivetran sync (Sync Now in dashboard, or wait for schedule).
#   4. Observe the JSON payload appear in your webhook.site tab.
#   5. CLEAN UP — once captured, delete the webhook:
#        bash scripts/capture_webhook_payload.sh --cleanup <webhook_id>
#      (the webhook_id is printed when the webhook is created)
#
# Why webhook.site: lets us inspect the payload without standing up the Cloud
# Run receiver yet. Once shape is verified, the receiver (ingest/webhook_
# receiver/main.py) can be deployed and pointed at by re-registering with
# its public URL.
#
# ── Manual HMAC signature verification (gotcha documented) ──────────────────
# Fivetran signs the raw wire body with HMAC-SHA-256 and sends the hex
# digest in `X-Fivetran-Signature-256` (no `sha256=` prefix, no timestamp
# header — verified live 2026-05-21).
#
# WEBHOOK.SITE DOWNLOAD GOTCHA: webhook.site's "download raw body" appends
# a trailing newline (one extra 0x0a byte) to the saved file. The wire body
# does NOT have this. If you HMAC the downloaded file directly you'll get a
# mismatch. Verify with:
#
#   SECRET='<from this script\'s output>' python3 -c 'import hmac,hashlib,os; \
#     b=open("/tmp/wh_body.bin","rb").read(); s=os.environ["SECRET"].encode(); \
#     print(f"len={len(b)} last={b[-1:].hex()}"); \
#     [print(f"hmac({k})={hmac.new(s,v,hashlib.sha256).hexdigest()}") \
#      for k,v in [("full",b),("first287",b[:287]),("rstripped",b.rstrip())]]'
#
# Expect: `last=0a`, `hmac(full)` ≠ header, `hmac(first287)` = header.
# (Also note: `VAR=value cmd` form — NO semicolon — exports VAR to cmd only;
# `VAR=value; cmd` is a shell variable not seen by the subprocess.)
#
# Reads FIVETRAN_API_KEY/SECRET from deploy/.env.

set -uo pipefail

API="https://api.fivetran.com"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "$SCRIPT_DIR")"
ENV_FILE="$ROOT/deploy/.env"

if [ -z "${FIVETRAN_API_KEY:-}" ] || [ -z "${FIVETRAN_API_SECRET:-}" ]; then
  if [ -f "$ENV_FILE" ]; then set -a; . "$ENV_FILE"; set +a; fi
fi
: "${FIVETRAN_API_KEY:?ERROR: FIVETRAN_API_KEY not set}"
: "${FIVETRAN_API_SECRET:?ERROR: FIVETRAN_API_SECRET not set}"

AUTH="$FIVETRAN_API_KEY:$FIVETRAN_API_SECRET"

# ── Cleanup mode ────────────────────────────────────────────────────────────
if [ "${1:-}" = "--cleanup" ]; then
  WEBHOOK_ID="${2:-}"
  : "${WEBHOOK_ID:?ERROR: pass the webhook_id to delete}"
  echo "Deleting webhook $WEBHOOK_ID ..."
  curl -s -u "$AUTH" -X DELETE "$API/v1/webhooks/$WEBHOOK_ID" | python3 -m json.tool
  exit 0
fi

# ── Create mode ─────────────────────────────────────────────────────────────
URL="${1:-}"
if [ -z "$URL" ] || [[ ! "$URL" =~ ^https:// ]]; then
  echo "Usage:"
  echo "  bash $0 <https://webhook.site/...>     # create webhook"
  echo "  bash $0 --cleanup <webhook_id>         # delete after capture"
  echo
  echo "The URL must be HTTPS — Fivetran rejects HTTP."
  exit 2
fi

# Generate a fresh HMAC secret. Fivetran signs payloads with this secret
# (SHA-256 HMAC, header X-Fivetran-Signature-256). Save it so you can verify
# signatures on the captured payload (matches ingest/webhook_receiver/main.py).
SECRET="$(openssl rand -hex 32)"

echo "=== Creating Fivetran account webhook ==="
echo "  URL    : $URL"
echo "  Events : sync_end"
echo "  Active : true"
echo "  Secret : $SECRET   ← save this if you want to verify signatures"
echo

PAYLOAD=$(python3 -c "
import json
print(json.dumps({
    'url': '$URL',
    'events': ['sync_end'],
    'active': True,
    'secret': '$SECRET',
}))")

RESP_FILE="$(mktemp)"
HTTP_CODE=$(curl -s -o "$RESP_FILE" -w '%{http_code}' \
  -u "$AUTH" \
  -X POST "$API/v1/webhooks/account" \
  -H 'Content-Type: application/json' \
  -d "$PAYLOAD")

echo "HTTP status: $HTTP_CODE"
echo "--- response ---"
python3 -m json.tool < "$RESP_FILE" || cat "$RESP_FILE"
echo

WEBHOOK_ID=$(python3 -c "
import json
try:
    d = json.load(open('$RESP_FILE'))
    print(d.get('data', {}).get('id', ''))
except Exception:
    print('')")

if [ -z "$WEBHOOK_ID" ]; then
  echo "Could not parse webhook_id from response — inspect above."
  exit 1
fi

cat <<EOF

=== Next steps ===
1. Open your webhook.site tab and keep it visible.
2. Trigger a Fivetran sync (dashboard → connector → Sync Now), or wait for
   the next scheduled sync_end (15 min frequency for assimilate_seem).
3. Observe the payload at $URL.
4. Capture/copy the JSON payload + the X-Fivetran-Signature-256 header.

When done, clean up:
  bash scripts/capture_webhook_payload.sh --cleanup $WEBHOOK_ID
EOF
