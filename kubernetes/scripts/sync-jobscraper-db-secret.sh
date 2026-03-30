#!/usr/bin/env bash
# Create or update secret jobscraper-db (namespace harco) with key DB_PASSWORD, matching
# kubernetes/harco/deployment.yaml secretKeyRef exactly.
#
# Upstream: CloudNativePG application secrets (e.g. your synced user secret pg-app-db in infra)
# usually expose the database password under the key "password". If your secret uses another
# field (e.g. pgpassword, or a custom operator label), set PASSWORD_KEY when invoking this script.
#
# Run once per cluster (or after DB password rotation).
#
# Override source/target if needed:
#   INFRA_SECRET_NAME   default: pg-app-db
#   INFRA_NAMESPACE     default: infra
#   PASSWORD_KEY        default: password
#   TARGET_NS           default: harco
#   TARGET_SECRET       default: jobscraper-db
#
set -euo pipefail

INFRA_SECRET_NAME="${INFRA_SECRET_NAME:-pg-app-db}"
INFRA_NAMESPACE="${INFRA_NAMESPACE:-infra}"
PASSWORD_KEY="${PASSWORD_KEY:-password}"
TARGET_NS="${TARGET_NS:-harco}"
TARGET_SECRET="${TARGET_SECRET:-jobscraper-db}"

if ! kubectl get secret "${INFRA_SECRET_NAME}" -n "${INFRA_NAMESPACE}" &>/dev/null; then
  echo "error: secret ${INFRA_SECRET_NAME} not found in namespace ${INFRA_NAMESPACE}" >&2
  exit 1
fi

PASSWORD="$(kubectl get secret "${INFRA_SECRET_NAME}" -n "${INFRA_NAMESPACE}" -o "jsonpath={.data.${PASSWORD_KEY}}" | base64 -d)"
if [[ -z "${PASSWORD}" ]]; then
  echo "error: empty password from ${INFRA_NAMESPACE}/${INFRA_SECRET_NAME} key ${PASSWORD_KEY}" >&2
  echo "hint: set PASSWORD_KEY if your secret uses a different field name" >&2
  exit 1
fi

kubectl create secret generic "${TARGET_SECRET}" \
  --namespace="${TARGET_NS}" \
  --from-literal=DB_PASSWORD="${PASSWORD}" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "updated ${TARGET_NS}/${TARGET_SECRET} from ${INFRA_NAMESPACE}/${INFRA_SECRET_NAME}"
