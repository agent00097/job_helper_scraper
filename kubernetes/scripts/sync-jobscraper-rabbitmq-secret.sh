#!/usr/bin/env bash
# Create or update secret jobscraper-rabbitmq (namespace harco).
#
# Keys match the Deployments: RABBITMQ_USER, RABBITMQ_PASSWORD.
#
# Usage (CI SSH step):
#   JOBSCRAPER_RABBITMQ_USER=job_worker \
#   JOBSCRAPER_RABBITMQ_PASSWORD=... \
#   ./kubernetes/scripts/sync-jobscraper-rabbitmq-secret.sh
#
# If either value is empty, leave the existing cluster secret unchanged.
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

NS="${TARGET_NS:-harco}"
TARGET_SECRET="${TARGET_SECRET:-jobscraper-rabbitmq}"
USER_VALUE="${JOBSCRAPER_RABBITMQ_USER:-}"
PASS_VALUE="${JOBSCRAPER_RABBITMQ_PASSWORD:-}"

if [ -z "${USER_VALUE// }" ] || [ -z "${PASS_VALUE// }" ]; then
  echo "JOBSCRAPER_RABBITMQ_USER/PASSWORD empty — leaving ${NS}/${TARGET_SECRET} unchanged"
  exit 0
fi

kubectl create secret generic "${TARGET_SECRET}" \
  --namespace "${NS}" \
  --from-literal="RABBITMQ_USER=${USER_VALUE}" \
  --from-literal="RABBITMQ_PASSWORD=${PASS_VALUE}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "updated ${NS}/${TARGET_SECRET} (RABBITMQ_USER, RABBITMQ_PASSWORD)"
