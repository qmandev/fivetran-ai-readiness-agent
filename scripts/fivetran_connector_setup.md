# Fivetran Connector Setup — Google Cloud PostgreSQL → BigQuery

Operational walkthrough for re-creating the Fivetran side of the schema-drift
sandbox. Assumes the Cloud SQL instance, DB user, BigQuery destination, and
IAM roles already exist — set those up via:

- `scripts/seed_test_db.sql` (Cloud SQL DB + read-only `fivetran` user + seed tables)
- `scripts/grant_fivetran_bq_roles.sh` (Fivetran SA IAM roles on the GCP project)

## 1. Connector type

Use **Google Cloud PostgreSQL** — NOT the generic PostgreSQL connector.
Fivetran detects a Cloud SQL host (Google-owned IP range) and rejects the
generic connector with: *"Use the GOOGLE specific connector instead of the
generic PostgreSQL connector."*

**Zombie-connector cleanup if you hit this error.** Fivetran's rejection
happens *after* the connector entry is created — so the failed-setup
connector persists in your account in an incomplete state. List
connections to find any zombies, then delete them explicitly:

```bash
source deploy/.env
# 1. List all connections
curl -s -u "$FIVETRAN_API_KEY:$FIVETRAN_API_SECRET" \
  https://api.fivetran.com/v1/connections \
  | python3 -c "import json,sys;[print(f\"{i['id']:28s} {i.get('service','?')}\") for i in json.load(sys.stdin).get('data',{}).get('items',[])]"
# 2. Delete the zombie (service=postgres, not google_cloud_postgresql)
curl -s -u "$FIVETRAN_API_KEY:$FIVETRAN_API_SECRET" \
  -X DELETE https://api.fivetran.com/v1/connections/<zombie-id>
```

`squid_contraction / fivetran_log` is Fivetran's auto-injected metadata
connector — always present, never the one to delete.

## 2. Connection configuration

| Field | Value |
|---|---|
| Destination | `fastAgentBigQuery` |
| Destination schema prefix | `ftar_pg` (→ BQ landing schema `ftar_pg_public`) |
| Host | `35.231.71.140` (Cloud SQL public IP — `gcloud sql instances describe ftar-pg`) |
| Port | `5432` |
| User | `fivetran` (created by `seed_test_db.sql`) |
| Password | `$FIVETRAN_DB_PW` (same value used in the seed; *never* hardcode in the repo) |
| Database | `appdb` |
| TLS / Require SSL | **ON** |
| Connection method | Direct (public IP) — matches our SaaS-mode authorized-networks setup |
| Incremental sync method | **Query-Based** (Decision #4; no replication slot needed) |

## 3. Connection tests → IAM mapping

If `Save & Test` fails on one of the four BigQuery permission tests, the
missing role is one of:

| Test failure                                                                | Required role            |
|-----------------------------------------------------------------------------|--------------------------|
| BigQuery User Permissions (`datasets.create` denied)                        | `roles/bigquery.user`    |
| BigQuery storage object admin Permissions                                   | `roles/storage.objectAdmin` |
| BigQuery Storage Object Admin Permissions For Unstructured File Bucket      | `roles/storage.objectAdmin` (same role; covers both buckets) |
| BigQuery Connection                                                         | any of the above suffices |

**Common pitfall:** granting only `bigquery.dataEditor + bigquery.jobUser`
(a natural-sounding pair) is **insufficient** — neither includes
`bigquery.datasets.create`. The role that contains it is `bigquery.user`.
Full grant set lives in `scripts/grant_fivetran_bq_roles.sh`.

## 4. IP allowlist (Cloud SQL authorized networks)

After the Postgres connector form is filled, Fivetran displays a section:
*"Please safelist the following Fivetran IPs / Host names in your firewall"*.

For Fivetran's GCP us-east4 processing region (chosen in destination setup),
this shows **8 consecutive IPs** plus a wildcard hostname:

```
35.234.176.144 .. 35.234.176.151
*.us-east4.gcp.proxy.prod.fivetran.com   (informational only — see below)
```

The 8 IPs collapse to a single CIDR — much cleaner than 8 `/32` entries:

```bash
gcloud sql instances patch ftar-pg \
  --authorized-networks="35.234.176.144/29"
```

Verify:

```bash
gcloud sql instances describe ftar-pg \
  --format="value(settings.ipConfiguration.authorizedNetworks[].value)"
# expected: 35.234.176.144/29
```

**Critical:** `--authorized-networks=` **REPLACES** the entire list (not
additive). Every IP/CIDR you want goes in one command.

**The wildcard hostname is informational, not actionable.** Cloud SQL
authorized-networks accepts only IPs/CIDR — not hostnames. The hostname
just confirms those IPs belong to Fivetran's us-east4 proxy
infrastructure (cross-check that we picked the right Fivetran processing
region in destination setup).

## 5. Schema tab — what to expect

| Observation | Interpretation |
|---|---|
| `customers` 5/5 columns, `orders` 6/6 columns | +1 each is `ctid` (Postgres tuple-location pseudo-column) — see below |
| `ctid` shown as primary key, **cannot be excluded** | Fivetran's row-tracking key for Query-Based without a replication slot. **Lands in BQ as a synthetic `ctid_fivetran_id` STRING column** (NOT raw `ctid`, NOT the docs' `_fivetran_id`). Our real PKs (`customer_id`, `order_id`) are unaffected and land normally. |
| Sync mode = **Soft delete mode** (default) | Leave as-is. Deleted source rows are retained in BQ with `_fivetran_deleted=TRUE`. Covered by `exclude_system_columns` in `app/tools/snapshot_diff.py`. |
| No explicit "cursor column" / "replication key" control | Auto-managed by the Google Cloud PostgreSQL connector under Query-Based. Nothing to configure manually. |
| Row filtering / Column hashing columns | Leave empty/off — neither affects schema-drift detection. |

## 6. Settings tab

- **Sync frequency: 15 minutes** (default is 6 hours).
- 15 min balances detection latency (target empirical metric) against MAR
  consumption on idle syncs.

## 7. Skip Activations

The "Configure Activation" step / tab is **reverse ETL** (warehouse → SaaS
operational tools like Salesforce, HubSpot, marketing platforms). Our agent
is forward-ETL-only (PG → BQ → agent → Fivetran transformations API).
Activations is also typically gated on paid tiers. Skip / dismiss.

## 8. Post-sync verification

- Status tab → **Sync now** → wait ~1–2 minutes for the 6 seed rows.
- Validate the landed schema in BigQuery — this is the live confirmation
  that `exclude_system_columns` targets the right names.

**Dataset name surprise.** The Google Cloud PostgreSQL connector ignores
the "Destination schema prefix" field and names the dataset purely after
the source schema — so for source `public` the dataset is `public`, NOT
`<prefix>_public`. List with:

```bash
bq ls --location=us-east1 api-project-910787152095:
```

**Every `bq` command on this dataset MUST pass `--location=us-east1`.**
The BigQuery CLI defaults to the `US` multi-region, and our dataset is in
the `us-east1` *region*. Omitting `--location` yields a confusing
"Dataset not found in location US" error even though the dataset exists.

Verification query:

```bash
bq query --location=us-east1 --use_legacy_sql=false --nouse_cache \
  "SELECT table_name, column_name, data_type, ordinal_position
   FROM \`api-project-910787152095.public.INFORMATION_SCHEMA.COLUMNS\`
   ORDER BY table_name, ordinal_position"
```

**Observed columns per table (verified live 2026-05-20):**

| Column | Type | Notes |
|---|---|---|
| `ctid_fivetran_id` | STRING | Fivetran tracking — **not** `_fivetran_id`; caught by the *suffix* branch of `exclude_system_columns` |
| real source columns | (mapped types) | `NUMERIC(10,2)` → BQ `BIGNUMERIC`; Postgres `TIMESTAMP` (no tz) → BQ `DATETIME` |
| `_fivetran_synced` | TIMESTAMP | Updated each sync — defeats the hash gate if NOT filtered |
| `_fivetran_deleted` | BOOL | Soft delete marker |

**Note on column ordering:** Fivetran does NOT preserve source column order
in the destination — `ctid_fivetran_id` lands first, then real columns in a
non-source order. The REORDER drift type compares successive *destination*
snapshots (not source vs destination), so this is correct as-is; just don't
assume source ordinal == destination ordinal.

The exclusion rule (`app/tools/snapshot_diff.py`):
```python
FIVETRAN_SYSTEM_PREFIX     = "_fivetran_"      # _fivetran_synced, _fivetran_deleted
FIVETRAN_SYSTEM_ID_SUFFIX  = "_fivetran_id"    # ctid_fivetran_id (this connector's variant)
```
A column is excluded iff it starts with the prefix OR ends with the suffix.

---

## 9. Receiver deployment (Cloud Run)

End-to-end deploy of `ingest/webhook_receiver/main.py:handle_request` to
Cloud Run via Cloud Build (`deploy/cloudbuild.yaml`). The build stages
are idempotent (ensure dataset → apply DDL → deploy receiver).

```bash
gcloud builds submit --config=deploy/cloudbuild.yaml
```

### 9a. Pre-deploy IAM / artifact checklist

Without these in place, the first deploy will burn ~90 minutes hitting
the layered failures documented in §9b. Do them ALL before the first
`gcloud builds submit`.

```bash
# Two dedicated service accounts (older projects auto-create defaults;
# newer projects do not — assume they're missing and create explicitly).
gcloud iam service-accounts create ftar-receiver-sa --project="${PROJECT_ID}"
gcloud iam service-accounts create ftar-build-sa    --project="${PROJECT_ID}"

# Receiver runtime SA — least-privilege for the Cloud Run service identity.
for R in roles/bigquery.dataEditor roles/bigquery.jobUser \
         roles/secretmanager.secretAccessor roles/aiplatform.user; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:ftar-receiver-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="$R"
done

# Build SA — needs cloudbuild + run.builder (THE magic role for source
# deploys from inside Cloud Build) + storage + AR + logging + BQ + iam.
for R in roles/cloudbuild.builds.builder roles/run.builder roles/run.admin \
         roles/storage.admin roles/artifactregistry.admin \
         roles/logging.logWriter roles/bigquery.dataEditor roles/bigquery.jobUser; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:ftar-build-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="$R"
done

# Build SA must be able to "act as" the receiver runtime SA at deploy
# time, and as the Compute default (used by the inner Buildpacks build).
gcloud iam service-accounts add-iam-policy-binding \
  "ftar-receiver-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --member="serviceAccount:ftar-build-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"
gcloud iam service-accounts add-iam-policy-binding \
  "$(gcloud projects describe ${PROJECT_ID} --format='value(projectNumber)')-compute@developer.gserviceaccount.com" \
  --member="serviceAccount:ftar-build-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountUser"

# Pre-create the AR repo so the first deploy doesn't fight propagation lag.
gcloud artifacts repositories create cloud-run-source-deploy \
  --repository-format=docker --location=us-east1 --project="${PROJECT_ID}"

# Pre-create the three Secret Manager secrets referenced by --set-secrets=
# in cloudbuild.yaml.  Each command also adds a version interactively
# (read -rs suppresses echo; printf "%s" avoids a trailing newline that
# would mismatch the HMAC byte-for-byte).
for S in fivetran-webhook-secret fivetran-api-key fivetran-api-secret; do
  gcloud secrets create "$S" --replication-policy=automatic --project="${PROJECT_ID}"
  read -rs "VAL?Value for $S (will NOT echo): " && echo
  printf "%s" "$VAL" | gcloud secrets versions add "$S" --data-file=- --project="${PROJECT_ID}"
  unset VAL
done
```

### 9b. The 6-layer unblock chain (recorded so the next deploy is fast)

Validated empirically 2026-05-23 — successful build
`7c6c4a5d-3c5f-4e6b-a4bd-5ec34300c588`, 5m3s. Each row below is a real
error we hit, in order, with the fix that unblocked it.

| # | Symptom (from build log) | Root cause | Fix |
|---|---|---|---|
| 1 | `Permission 'artifactregistry.repositories.create' denied` on first deploy | First-run repo creation lacked `admin`; `writer` insufficient. | Grant `roles/artifactregistry.admin` to build SA (or pre-create `cloud-run-source-deploy` AR repo). |
| 2 | `caller does not have permission to act as service account projects/.../<NUMERIC_ID>` | Outer build SA lacks the `actAs` permission gated by `roles/run.builder`. The error wording is misleading — adding individual act-as bindings on the Compute default or building loop-back bindings does **not** substitute. `--build-service-account=…` is silently ignored in `--function=` mode. | Grant `roles/run.builder` to outer build SA at project level. THIS IS THE BIG ONE. |
| 3 | `missing main.py and GOOGLE_FUNCTION_SOURCE not specified` from Functions-Framework buildpack | Buildpack scans repo root for `main.py`; the receiver lives in `ingest/webhook_receiver/main.py`. | Add a thin `main.py` at the repo root that re-exports the handler (`from ingest.webhook_receiver.main import handle_request`). Set `--function=handle_request` (bare name, no dotted path). |
| 4 | `pyproject.toml ... functions-framework not in your dependencies` | Buildpack reads `pyproject.toml`, NOT the nested `ingest/webhook_receiver/requirements.txt`. | Add `functions-framework>=3.0` to `[project] dependencies` in `pyproject.toml`; run `uv sync` locally to refresh `uv.lock`; commit both. |
| 5 | Buildpack picks Python 3.14.x (ignores `requires-python = ">=3.11,<3.14"` in pyproject), breaking `uv sync` against pinned deps | `requires-python` is metadata for tools like `pip`/`uv` to honor — Buildpack's runtime selector ignores it and picks "latest". | Pin via `--set-build-env-vars=GOOGLE_RUNTIME_VERSION=3.13` in `cloudbuild.yaml` deploy step. |
| 6 | `Creating Revision ... Secret projects/.../secrets/<X>/versions/latest was not found` | Secrets named in `--set-secrets=` don't exist yet. | Pre-create all three secrets with initial versions (see §9a final loop). Runtime SA needs `roles/secretmanager.secretAccessor` at project level (already granted in §9a). |

### 9c. Bonus traps discovered along the way

- **Inner Buildpacks builds are regional** (e.g., `us-east1`), outer
  Cloud Build is global. `gcloud builds list` defaults to global; use
  `--region=us-east1` to find the inner build IDs:
  ```bash
  gcloud builds list --project="${PROJECT_ID}" --region=us-east1 --limit=5
  gcloud builds log <INNER_ID> --project="${PROJECT_ID}" --region=us-east1
  ```
- **The `gcr.io/cloud-builders/gcloud` image** lags on the `--build-service-account` flag for `--function=` mode — don't waste time pinning the inner SA; granting `roles/run.builder` to the outer build SA is the correct path.
- **IAM propagation lag** (~2-7 min for fresh role bindings) is real but rarely the load-bearing cause when the same error fires 3+ times. After the third repeat, look for a different layer, not a longer wait.
- **Newer GCP projects** (post-May-2024) don't auto-create the legacy `PROJECT_NUMBER@cloudbuild.gserviceaccount.com` or `PROJECT_NUMBER-compute@developer.gserviceaccount.com` default SAs eagerly — they appear lazily when their respective APIs are enabled (`cloudbuild.googleapis.com`, etc.).
- **`uv sync` locally before `gcloud builds submit`** — keeps `uv.lock` in sync with `pyproject.toml`. The buildpack runs `uv sync --frozen` and fails if they're out of date.

### 9d. URL formats — old vs canonical

Cloud Run is migrating service URLs from the random-suffix format to a
project-number/region format. Both route to the same service for now,
but the canonical (new) form should be used in any new config.

| Format | Example | Status |
|---|---|---|
| Canonical (new) | `https://fivetran-sync-end-receiver-910787152095.us-east1.run.app` | **Use this.** Returned by `gcloud run services describe ... --format="value(status.url)"`. |
| Legacy | `https://fivetran-sync-end-receiver-hnwsyfwfiq-ue.a.run.app` | Still routes to the same service, but being phased out. |

### 9e. `--min-instances=1` (always-warm)

```bash
gcloud run services update fivetran-sync-end-receiver \
  --region=us-east1 --project="${PROJECT_ID}" --min-instances=1
```

Mandatory for this service. A Python + `google-adk` cold start is
15–30s, longer than Fivetran's webhook-test endpoint timeout. Without
min-instances, registration POSTs fail with `InvalidInput: Read timed
out` (the receiver does eventually return 200, but Fivetran's client
has given up by then). Real `sync_end` deliveries hit the same
cold-start variability.

**Cost:** ~$2–5/month for the always-warm instance. **Fully covered**
by either of the two scoped hackathon credits — Cloud Run is in the
FAQ's named-services list. Across the Rapid Agent Hackathon credit's
60-day window ($100 budget, valid through 2026-07-18) this represents
~10% of the credit; after that, the GenAI App Builder trial credit
($1000, valid through 2027-05-09) continues to cover it. See the
**Operational Costs & Credit Coverage** section of
`fivetranAgentDesign.md` for the full coverage map (which components
draw which credit, and the one ⚠ UNCONFIRMED gap around Cloud SQL).

---

## 10. Webhook registration (REST API only)

**Fivetran does not support webhook registration via the UI.** The
classic dashboard's Account Settings page does not have a Webhooks tab.
Registration is exclusively via the REST API.

### 10a. Reference

- Docs: <https://fivetran.com/docs/rest-api/webhooks>
- API reference (create-account-webhook):
  <https://fivetran.com/docs/rest-api/api-reference/webhooks/create-account-webhook>
- Endpoint: `POST https://api.fivetran.com/v1/webhooks/account`
- Auth: HTTP Basic (`api-key:api-secret`)
- Required body fields: `url`, `events`. Optional: `active` (bool), `secret` (string).

### 10b. Register the account webhook

Single line, all secrets pulled from Secret Manager so nothing
sensitive is typed:

```bash
API_KEY=$(gcloud secrets versions access latest --secret=fivetran-api-key --project="${PROJECT_ID}"); API_SECRET=$(gcloud secrets versions access latest --secret=fivetran-api-secret --project="${PROJECT_ID}"); WEBHOOK_SECRET=$(gcloud secrets versions access latest --secret=fivetran-webhook-secret --project="${PROJECT_ID}"); curl -sS -u "${API_KEY}:${API_SECRET}" -H "Content-Type: application/json" -X POST https://api.fivetran.com/v1/webhooks/account -d "{\"url\":\"https://fivetran-sync-end-receiver-910787152095.us-east1.run.app\",\"events\":[\"sync_end\"],\"active\":true,\"secret\":\"${WEBHOOK_SECRET}\"}"; unset API_KEY API_SECRET WEBHOOK_SECRET
```

Expected response (`200`):

```json
{
  "code": "Success",
  "message": "Account webhook has been created",
  "data": {
    "id": "undergoing_lat",
    "type": "account",
    "url": "https://fivetran-sync-end-receiver-910787152095.us-east1.run.app",
    "events": ["sync_end"],
    "active": true,
    "secret": "******",
    "created_at": "2026-05-24T01:03:32.593000Z",
    "created_by": "arsenic_abbreviated"
  }
}
```

- `data.secret = "******"` is **masked output**, not a rejection — the
  secret we supplied is stored and used for HMAC signing.
- The returned `data.id` (here: `undergoing_lat`) is the handle for
  GET / PATCH / DELETE later. Save it.

### 10c. Failure mode: `InvalidInput: Read timed out`

If you see this from the create call:

```json
{"code":"InvalidInput","message":"Failed to call webhook endpoint: 'java.net.SocketTimeoutException: Read timed out'"}
```

Fivetran tests the endpoint synchronously during registration. The
receiver's cold start (Python + `google-adk` = ~15–30s) exceeded
Fivetran's test timeout. Cloud Run server-side logs show the request
DID return 200 — but slowly. The fix is §9e (`--min-instances=1`). The
webhook record is **not** persisted when this fires; safe to retry after
applying the fix.

### 10d. Inspect / modify / delete the registered webhook

```bash
# List all webhooks on the account
API_KEY=$(gcloud secrets versions access latest --secret=fivetran-api-key --project="${PROJECT_ID}"); API_SECRET=$(gcloud secrets versions access latest --secret=fivetran-api-secret --project="${PROJECT_ID}"); curl -sS -u "${API_KEY}:${API_SECRET}" https://api.fivetran.com/v1/webhooks; unset API_KEY API_SECRET

# Get one
curl -sS -u "${API_KEY}:${API_SECRET}" https://api.fivetran.com/v1/webhooks/undergoing_lat

# Disable without deleting (active=false)
curl -sS -u "${API_KEY}:${API_SECRET}" -H "Content-Type: application/json" -X PATCH \
  https://api.fivetran.com/v1/webhooks/undergoing_lat -d '{"active":false}'

# Delete permanently
curl -sS -u "${API_KEY}:${API_SECRET}" -X DELETE https://api.fivetran.com/v1/webhooks/undergoing_lat
```

### 10e. Currently registered (2026-05-24)

| Field | Value |
|---|---|
| ID | `undergoing_lat` |
| Type | `account` |
| URL | `https://fivetran-sync-end-receiver-910787152095.us-east1.run.app` |
| Events | `["sync_end"]` |
| Active | `true` |
| Created | `2026-05-24T01:03:32Z` |

---

## 11. End-to-end smoke test

Trigger a Fivetran sync (manual "Sync now" or wait for the 15-minute
scheduled cycle), then in parallel watch:

```bash
# Cloud Run receiver logs — confirm webhook arrived + HMAC verified + dispatched
gcloud run services logs read fivetran-sync-end-receiver \
  --region=us-east1 --project="${PROJECT_ID}" --limit=50

# BigQuery drift_events — confirm pipeline wrote a row (only fires on real drift)
bq query --location=us-east1 --use_legacy_sql=false \
  "SELECT detected_at, connection_id, table_name, change_type, classification, confidence
   FROM \`api-project-910787152095.agent_state.drift_events\`
   ORDER BY detected_at DESC LIMIT 5"
```

**No drift_events rows after a sync ≠ broken.** The `content_hash` gate
in `capture_and_gate()` is designed to suppress no-op events. To force a
row, `ALTER TABLE customers ADD COLUMN test_drift TEXT;` on the source PG
between two syncs.
