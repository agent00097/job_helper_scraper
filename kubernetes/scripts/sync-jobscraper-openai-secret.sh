#!/usr/bin/env bash
# Create or update secret jobscraper-openai (namespace harco) with key OPENAI_API_KEY.
#
# Usage (server / CI SSH step):
#   OPENAI_API_KEY=sk-... ./kubernetes/scripts/sync-jobscraper-openai-secret.sh
#
# If OPENAI_API_KEY is empty, the script exits 0 without changing the cluster
# (deployments mark the env ref optional so alias-only extraction still works).
set -euo pipefail

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

NS="${TARGET_NS:-harco}"
TARGET_SECRET="${TARGET_SECRET:-jobscraper-openai}"
KEY_NAME="OPENAI_API_KEY"
VALUE="${OPENAI_API_KEY:-}"

if [ -z "${VALUE// }" ]; then
  echo "OPENAI_API_KEY empty — leaving ${NS}/${TARGET_SECRET} unchanged (embeddings disabled until set)"
  exit 0
fi

kubectl create secret generic "${TARGET_SECRET}" \
  --namespace "${NS}" \
  --from-literal="${KEY_NAME}=${VALUE}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "updated ${NS}/${TARGET_SECRET} (${KEY_NAME})"
