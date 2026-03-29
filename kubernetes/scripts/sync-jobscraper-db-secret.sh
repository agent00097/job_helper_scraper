#!/usr/bin/env bash
# Create or update secret jobscraper-db in harco with DB_PASSWORD copied from the
# CloudNativePG application secret in infra. Run once per cluster (or after DB password rotation).
#
# Override key names if your pg-app-db secret uses different fields:
#   INFRA_SECRET_NAME   default: pg-app-db
#   INFRA_NAMESPACE     default: infra
#   PASSWORD_KEY        default: password
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
