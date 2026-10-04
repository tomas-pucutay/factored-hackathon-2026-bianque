#!/usr/bin/env bash
# Deploy the API to Google Cloud Run (make deploy).
#
# Reads only the GCP_* variables from .env (never exports the other secrets), checks the
# serving slice exists, and builds the image remotely with Cloud Build from the files allowed
# by .gcloudignore. Prints the service URL at the end.
set -euo pipefail

if [[ -f .env ]]; then
    while IFS='=' read -r key value; do
        export "$key=$value"
    done < <(grep -E '^GCP_[A-Z_]+=' .env)
fi
: "${GCP_PROJECT_ID:?set GCP_PROJECT_ID in .env (see .env.example)}"
REGION="${GCP_REGION:-us-central1}"
SERVICE="${GCP_SERVICE:-bianque-api}"

if [[ ! -f data/gold/serving/serving.duckdb ]]; then
    echo "data/gold/serving/serving.duckdb is missing: run make gold first" >&2
    exit 1
fi

# Capacity limits: at most 2 instances, 40 concurrent requests each; scales to zero when idle.
gcloud run deploy "$SERVICE" \
    --source . \
    --project "$GCP_PROJECT_ID" \
    --region "$REGION" \
    --allow-unauthenticated \
    --cpu 1 \
    --memory 512Mi \
    --min-instances 0 \
    --max-instances 2 \
    --concurrency 40 \
    --timeout 60 \
    --quiet

gcloud run services describe "$SERVICE" --project "$GCP_PROJECT_ID" --region "$REGION" \
    --format 'value(status.url)'
