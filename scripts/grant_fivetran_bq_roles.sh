#!/usr/bin/env bash
# Grant the Fivetran service account the IAM roles its BigQuery destination
# setup test requires. Idempotent — `add-iam-policy-binding` is safe to re-run.
#
# ── Troubleshooting reference (Fivetran connection test → missing role) ─────
# Fivetran's BigQuery destination "Save & Test" runs four checks. If any fail,
# the role mapping is:
#
#   Test failure                                              | Required role
#   ----------------------------------------------------------|--------------------------
#   "BigQuery User Permissions" — datasets.create denied      | roles/bigquery.user
#   "BigQuery storage object admin Permissions"               | roles/storage.objectAdmin
#   "BigQuery Storage Object Admin Permissions For            | roles/storage.objectAdmin
#       Unstructured File Bucket"                             |   (same role; covers both
#                                                             |    staging + unstructured)
#   "BigQuery Connection"                                     | any of the above suffices
#
# Fivetran's published setup guide also lists bigquery.dataEditor and
# bigquery.jobUser explicitly. jobUser is redundant with bigquery.user, but
# both are granted here to match the docs exactly and avoid edge cases.
#
# Common pitfall this script avoids: granting only `bigquery.dataEditor +
# bigquery.jobUser` (a natural-sounding pair) is INSUFFICIENT — neither
# includes `bigquery.datasets.create`, so the User Permissions test fails.
# `bigquery.user` is the role that contains datasets.create + jobs.create.
#
# ── Usage ───────────────────────────────────────────────────────────────────
#   export FIVETRAN_SA_EMAIL='<from Fivetran destination setup page>'
#   # GCP_PROJECT_ID is optional; defaults to the project provisioned in this
#   # repo. Override if you point Fivetran at a different project.
#   export GCP_PROJECT_ID='api-project-910787152095'
#   bash scripts/grant_fivetran_bq_roles.sh
#
# Allow ~30s after the script finishes for IAM propagation, then click
# "Save & Test" again in Fivetran.

set -euo pipefail

: "${FIVETRAN_SA_EMAIL:?ERROR: export FIVETRAN_SA_EMAIL=... (find it on the Fivetran destination setup page)}"
PROJECT="${GCP_PROJECT_ID:-api-project-910787152095}"

ROLES=(
  "roles/bigquery.user"          # datasets.create + jobs.create (the usually-missing one)
  "roles/bigquery.dataEditor"    # write data into datasets
  "roles/bigquery.jobUser"       # redundant w/ bigquery.user, but in Fivetran's docs
  "roles/storage.objectAdmin"    # GCS staging bucket + unstructured-file bucket
)

echo "Granting roles to ${FIVETRAN_SA_EMAIL} on project ${PROJECT}:"
for role in "${ROLES[@]}"; do
  echo "  - ${role}"
  gcloud projects add-iam-policy-binding "${PROJECT}" \
    --member="serviceAccount:${FIVETRAN_SA_EMAIL}" \
    --role="${role}" \
    --condition=None \
    --quiet >/dev/null
done

echo
echo "Done. Wait ~30s for IAM propagation, then re-run Fivetran's Save & Test."
