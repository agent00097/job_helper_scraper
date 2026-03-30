# Job Helper Scraper

A service for fetching jobs from multiple sources (APIs and scrapers) and storing them in a PostgreSQL database.

## Features

- **Multi-source support**: Fetch jobs from Greenhouse, Ashby, Lever, and more
- **API-first approach**: Start with API sources (no scraping needed)
- **Automatic deduplication**: Prevents duplicate jobs across sources
- **Periodic scheduling**: Runs sources on configurable schedules
- **Database-driven configuration**: All sources configured in PostgreSQL
- **Rate limiting**: Respects API rate limits per source

## Setup

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

### 2. Database Setup

Create the required database tables:

```bash
cd schemas
./setup_all_schemas.sh
```

Or manually:

```bash
psql $DATABASE_URL -f schemas/create_job_sources_table.sql
psql $DATABASE_URL -f schemas/create_source_credentials_table.sql
psql $DATABASE_URL -f schemas/create_source_companies_table.sql
psql $DATABASE_URL -f schemas/add_content_hash_to_jobs.sql
psql $DATABASE_URL -f schemas/setup_greenhouse_source.sql
```

### 3. Environment Variables

Create a `.env` file:

```bash
DATABASE_URL=postgresql://user:password@host:port/database
```

For production-style discrete variables (recommended when passwords contain special characters), you can set `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, and `DB_PASSWORD` instead; see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

### 4. Test Greenhouse API

Test that Greenhouse API is working:

```bash
python test_greenhouse.py
```

## Usage

### Run the Service

Start the scheduler to run all enabled sources:

```bash
python main.py
```

The service will:
- Load all enabled sources from the database
- Run each source on its configured schedule
- Fetch jobs from all companies for each source
- Save jobs to the database (with deduplication)

### Add More Companies

Add companies to scrape for Greenhouse:

```sql
INSERT INTO source_companies (source_id, company_name, company_endpoint, enabled)
SELECT id, 'Company Name', 'company-slug', TRUE
FROM job_sources WHERE name = 'greenhouse';
```

Find company slugs by visiting: `https://boards.greenhouse.io/{company-slug}`

## Architecture

```
main.py
  └── scheduler.py
       └── source_worker.py
            └── sources/
                 ├── base_source.py (abstract)
                 ├── api/
                 │   └── greenhouse_source.py
                 └── scraper/ (for future)
```

## Database Schema

- **job_sources**: Configuration for each source
- **source_credentials**: API keys and authentication
- **source_companies**: Companies to fetch jobs from
- **jobs**: Stored job listings
- **job_keywords**: Keywords for filtering (future use)

## Current Sources

### Greenhouse (API)
- **Status**: ✅ Implemented
- **API Key**: Not required (public API)
- **Rate Limit**: 60 requests/minute
- **Schedule**: Every 6 hours

## Adding New Sources

1. Create a new source class inheriting from `BaseSource`
2. Implement `fetch_jobs()` method
3. Add source to `source_factory.py`
4. Insert source config into `job_sources` table

See `docs/` for more detailed documentation.

## Production: Kubernetes (k3s on Hetzner) and CI/CD

This service is a **long-running Python worker** (`main.py` → `Scheduler` → background threads). It is **not** an HTTP server.

**Keep this worker single-replica.** The Deployment is committed with `replicas: 1`. Scaling above one runs multiple schedulers and **duplicates scraping** against the same sources and database. There is **no Service** and **no Ingress** in this repo because the code does not expose an HTTP port.

### Probes (no HTTP)

The Deployment **intentionally has no `livenessProbe` or `readinessProbe`**. The process does not serve HTTP, so an HTTP probe would be misleading. An `exec` liveness probe (e.g. `pgrep python`) was not added: if a scrape runs for a long time, a naive process check can still be a poor signal, and **if the main process exits, the container exits** anyway. See comments in [`kubernetes/harco/deployment.yaml`](kubernetes/harco/deployment.yaml).

### One-time prerequisites (checklist)

Before the first successful workflow run:

1. **Deploy user on the Hetzner server** has a **kubeconfig** (e.g. default `~/.kube/config`) that can `kubectl apply` to namespace `harco` on your k3s cluster.
2. **Server-side Git** can fetch this repo over **SSH** (or another method you wire into `git fetch`): deploy key, SSH agent, or equivalent so `git fetch origin main` succeeds in the checkout directory.
3. Namespace **`harco`** exists and **`ghcr-pull-secret`** is present there (private GHCR pulls).
4. PostgreSQL reachable at `app-postgres-rw.infra.svc.cluster.local:5432`, database `resume_jobs`, user `resume_user`; SQL schemas applied (see [Database Setup](#2-database-setup)).
5. Secret **`jobscraper-db`** in **`harco`** with key **`DB_PASSWORD`**, synced from CloudNativePG (see below).

**DB password sync:** CloudNativePG application secrets (e.g. `pg-app-db` in `infra`) typically store the password under the key **`password`**. If yours differs, run the sync script with `PASSWORD_KEY=...` (see [`kubernetes/scripts/sync-jobscraper-db-secret.sh`](kubernetes/scripts/sync-jobscraper-db-secret.sh)).

```bash
./kubernetes/scripts/sync-jobscraper-db-secret.sh
```

6. Clone this repository on the server at the path you set in **`HETZNER_REPO_PATH`** (GitHub variable below).

### GitHub configuration (aligned with backend/frontend naming)

**Secrets (repository)**

| Secret | Purpose |
|--------|---------|
| `HETZNER_SSH_KEY` | Private key for SSH from Actions to the Hetzner server (cluster API is not exposed to GitHub). |

`GITHUB_TOKEN` is provided automatically for GHCR login and push.

**Variables (repository)**

| Variable | Required | Purpose |
|----------|----------|---------|
| `HETZNER_HOST` | Yes | Server hostname or IP for SSH. |
| `HETZNER_USER` | Yes | SSH user (must have `kubectl` and kubeconfig for the cluster). |
| `HETZNER_REPO_PATH` | Yes | Absolute path to this repo on the server (e.g. `/home/deploy/job-helper-scraper`). |

Optional: if SSH is not on port 22, add a `port:` line to the `appleboy/ssh-action` step in [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml).

### What CI does (deploy method)

Triggers on **every push to `main`** (see [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml)).

1. Builds the image from the repo root [`Dockerfile`](Dockerfile).
2. Pushes to GHCR as `ghcr.io/<lowercase-owner>/<repo>:<sha>` and `:latest`.
3. SSHs to Hetzner, **`cd` to the server checkout** (`HETZNER_REPO_PATH`), `git fetch` / `git reset --hard origin/main`.
4. Runs [`kubernetes/harco/deploy.sh`](kubernetes/harco/deploy.sh) with the **commit SHA image** only (`...:<github.sha>`), not `:latest`.

**Why manifests from the server repo:** the same files you version in Git are what `kubectl apply` uses after `git reset --hard origin/main`, so deploys stay reproducible and match `main`. The Deployment manifest keeps a `PLACEHOLDER_IMAGE`; `deploy.sh` **`sed`-substitutes the real `ghcr.io/...:<sha>`** and pipes YAML to `kubectl apply`, so Kubernetes never needs a manual `kubectl set image` and the running tag is always the SHA built in that workflow run.

5. **`deploy.sh`** prints `kubectl get deployment` / `kubectl get pods`, runs **`kubectl rollout status`**, prints them again on success, and on rollout failure prints **`kubectl describe deployment`** and **recent `kubectl get events`**.

Kubernetes API access stays on the server only.

### Resource requests and limits

In [`kubernetes/harco/deployment.yaml`](kubernetes/harco/deployment.yaml): **requests** `cpu: 100m`, `memory: 256Mi`; **limits** `cpu: 1000m`, `memory: 768Mi`. Rationale: keep a small baseline on a single-node host while allowing bursts during HTTP fetches and DB writes; the memory limit headroom reduces OOM risk when descriptions or batches are large.

### Kubernetes objects (namespace `harco`)

| Object | Name | Role |
|--------|------|------|
| ConfigMap | `jobscraper-config` | Non-secret DB settings + `PGSSLMODE` |
| Secret | `jobscraper-db` | `DB_PASSWORD` only (sync from `infra/pg-app-db`) |
| Deployment | `jobscraper` | Single replica; `imagePullSecrets: ghcr-pull-secret` |

There is **no Service** or **Ingress** in this repository.

**Effective container environment** (Kubernetes):

- From ConfigMap `jobscraper-config`: `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `PGSSLMODE`
- From Secret `jobscraper-db` key `DB_PASSWORD`: `DB_PASSWORD`

### Operations

```bash
kubectl logs -f deployment/jobscraper -n harco
kubectl rollout restart deployment/jobscraper -n harco
```

More detail: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
