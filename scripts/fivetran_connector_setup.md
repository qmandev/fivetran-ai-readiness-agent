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
