#!/usr/bin/env bash
# Apply ConfigMap + Deployment with the image tag built in CI.
# Usage: ./deploy.sh <full-image-reference>
# Example: ./deploy.sh ghcr.io/myorg/job-helper-scraper:abc123def
set -euo pipefail

IMAGE="${1:?Usage: $0 <full-image-reference>}"
ROOT="$(cd "$(dirname "$0")" && pwd)"

kubectl apply -f "${ROOT}/configmap.yaml"
sed "s|PLACEHOLDER_IMAGE|${IMAGE}|g" "${ROOT}/deployment.yaml" | kubectl apply -f -
kubectl rollout status deployment/jobscraper -n harco --timeout=300s
