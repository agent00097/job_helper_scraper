# Deployment Guide

## Runtime model

- **Stack**: Python 3.11, `psycopg`, `requests` (see [`requirements.txt`](../requirements.txt) and [`Dockerfile`](../Dockerfile)).
- **Process**: [`main.py`](../main.py) starts a [`Scheduler`](../scheduler.py) that loads enabled sources from the database and runs [`SourceWorker`](../workers/source_worker.py) threads on a loop (default check every 60 seconds, per-source interval from `schedule_hours` in `job_sources`).
- **Workload**: **Deployment** (continuous worker), not a CronJob—the scheduler is designed to stay up and re-run sources on DB-driven schedules.
- **HTTP**: None in this codebase; no Service or Ingress is required.
- **Probes**: No HTTP health endpoints; manifests intentionally omit liveness/readiness probes to avoid fake checks.
- **Storage**: Stateless; no PersistentVolume. All config and state live in PostgreSQL.
- **Other dependencies**: Outbound HTTPS to public job APIs (e.g. Greenhouse). No Redis or extra services.

## Docker

Build and run locally:

```bash
docker build -t job-helper-scraper:local .
docker run --rm -e DATABASE_URL=... job-helper-scraper:local
```

For Kubernetes, prefer `DB_*` variables (see [`db.py`](../db.py)) so passwords with special characters are safe.

## Kubernetes (production layout)

Manifests live under [`kubernetes/harco/`](../kubernetes/harco/):

- [`configmap.yaml`](../kubernetes/harco/configmap.yaml) — non-secret DB settings and `PGSSLMODE`
- [`deployment.yaml`](../kubernetes/harco/deployment.yaml) — Deployment `jobscraper`; image placeholder `PLACEHOLDER_IMAGE` is replaced by [`deploy.sh`](../kubernetes/harco/deploy.sh)

[`kustomization.yaml`](../kubernetes/harco/kustomization.yaml) lists the same resources for optional `kubectl apply -k` use; the CI path uses `deploy.sh` so the image tag is always the commit SHA.

### Database secret sync

Create/update `harco/jobscraper-db` from `infra/pg-app-db`:

```bash
./kubernetes/scripts/sync-jobscraper-db-secret.sh
```

### Manual deploy (server)

From the repo root on the node that has kubeconfig:

```bash
./kubernetes/harco/deploy.sh 'ghcr.io/<owner>/<repo>:<tag>'
```

## CI/CD

Workflow: [`.github/workflows/deploy.yml`](../.github/workflows/deploy.yml). It runs on **push to `main`**.

Repository **variables**: `HETZNER_HOST`, `HETZNER_USER`, `HETZNER_REPO_PATH`. Repository **secret**: `HETZNER_SSH_KEY`.

The job builds and pushes `ghcr.io/<repo>:<sha>` (and `:latest`), then SSHs to the server, resets the checkout to `origin/main`, and runs [`kubernetes/harco/deploy.sh`](../kubernetes/harco/deploy.sh) with the **SHA tag** only. Manifests are applied from that checkout (`kubectl apply` + `sed` for the image line); see the [README Production section](../README.md#production-kubernetes-k3s-on-hetzner-and-cicd).

### CloudNativePG password key

[`sync-jobscraper-db-secret.sh`](../kubernetes/scripts/sync-jobscraper-db-secret.sh) reads the upstream secret key **`password`** by default. If `pg-app-db` (or your source secret) uses a different data key, set **`PASSWORD_KEY`** when running the script.

## Updating configuration

Edit the ConfigMap and reapply:

```bash
kubectl apply -f kubernetes/harco/configmap.yaml
kubectl rollout restart deployment/jobscraper -n harco
```

## Troubleshooting

- **ImagePullBackOff**: Check `ghcr-pull-secret` in `harco` and that the image name matches `ghcr.io/<lowercase-owner>/<repo>:<tag>`.
- **DB connection**: Confirm `jobscraper-db` exists, network policy allows pods in `harco` to reach `infra`, and `PGSSLMODE` matches what CNPG expects.
- **No scraping**: Ensure rows exist in `job_sources` / `source_companies` and sources are `enabled` (see main [README](../README.md)).
