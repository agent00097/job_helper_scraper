#!/usr/bin/env bash
# Apply manifests from this directory on the server (checked-out repo). deploy.sh pipes a
# sed-substituted Deployment so the live image is the CI-built tag (commit SHA), not :latest.
# Usage: ./deploy.sh <full-image-reference>
# Example: ./deploy.sh ghcr.io/myorg/job-helper-scraper:abc123def
set -euo pipefail
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

IMAGE="${1:?Usage: $0 <full-image-reference>}"
ROOT="$(cd "$(dirname "$0")" && pwd)"
NS="harco"

kubectl apply -f "${ROOT}/configmap.yaml"
sed "s|PLACEHOLDER_IMAGE|${IMAGE}|g" "${ROOT}/deployment.yaml" | kubectl apply -f -
sed "s|PLACEHOLDER_IMAGE|${IMAGE}|g" "${ROOT}/deployment-rabbitmq-worker.yaml" | kubectl apply -f -

kubectl get deployment jobscraper -n "${NS}"
kubectl get pods -n "${NS}" -l app=jobscraper

if ! kubectl rollout status "deployment/jobscraper" -n "${NS}" --timeout=300s; then
  kubectl describe deployment jobscraper -n "${NS}" || true
  kubectl get events -n "${NS}" --sort-by=.lastTimestamp | tail -n 30 || true
  exit 1
fi

kubectl get deployment jobscraper -n "${NS}"
kubectl get pods -n "${NS}" -l app=jobscraper

kubectl get deployment jobscraper-rabbitmq-worker -n "${NS}" || true
if ! kubectl rollout status "deployment/jobscraper-rabbitmq-worker" -n "${NS}" --timeout=300s; then
  kubectl describe deployment jobscraper-rabbitmq-worker -n "${NS}" || true
  kubectl get events -n "${NS}" --sort-by=.lastTimestamp | tail -n 30 || true
  exit 1
fi

kubectl get deployment jobscraper-rabbitmq-worker -n "${NS}"
kubectl get pods -n "${NS}" -l app=jobscraper-rabbitmq-worker
