#!/usr/bin/env bash
# Store the API's secrets in Google Secret Manager (make deploy-secrets). Idempotent: an
# existing secret gets a new version. Values are read from .env and piped to gcloud; they are
# never printed or passed as command-line arguments.
#
#   GEMINI_API_KEY -> secret gemini-api-key
#   SESSION_SECRET -> secret session-secret
#
# Cloud Run's runtime service account gets read access to these two secrets only.
set -euo pipefail

value() { grep -E "^$1=" .env | head -1 | cut -d= -f2- | tr -d '\n'; }

PROJECT="$(value GCP_PROJECT_ID)"
: "${PROJECT:?set GCP_PROJECT_ID in .env}"
NUMBER="$(gcloud projects describe "$PROJECT" --format 'value(projectNumber)')"
RUNTIME_SA="${NUMBER}-compute@developer.gserviceaccount.com"

gcloud services enable secretmanager.googleapis.com --project "$PROJECT"

for pair in GEMINI_API_KEY:gemini-api-key SESSION_SECRET:session-secret; do
    var="${pair%%:*}"
    secret="${pair##*:}"
    if [[ -z "$(value "$var")" ]]; then
        echo "$var is empty in .env" >&2
        exit 1
    fi
    if gcloud secrets describe "$secret" --project "$PROJECT" >/dev/null 2>&1; then
        value "$var" | gcloud secrets versions add "$secret" --data-file=- --project "$PROJECT"
    else
        value "$var" | gcloud secrets create "$secret" --data-file=- \
            --replication-policy=automatic --project "$PROJECT"
    fi
    gcloud secrets add-iam-policy-binding "$secret" --project "$PROJECT" \
        --member "serviceAccount:${RUNTIME_SA}" --role roles/secretmanager.secretAccessor \
        --condition=None >/dev/null
    echo "$secret: stored, readable by the Cloud Run runtime service account"
done
