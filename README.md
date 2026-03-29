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

This service is a **long-running Python worker** (`main.py` → `Scheduler` → background threads). It is **not** an HTTP server. Deploy it as a **single-replica Deployment** in namespace `harco`, with **no Ingress**. Use **one replica only** so the same sources are not scraped concurrently.

### One-time cluster prerequisites

1. Namespace `harco` exists and `ghcr-pull-secret` is present (for private GHCR pulls).
2. PostgreSQL reachable at `app-postgres-rw.infra.svc.cluster.local:5432`, database `resume_jobs`, user `resume_user`.
3. Apply SQL schemas to that database (see [Database Setup](#2-database-setup)).
4. Create the app DB password secret in `harco` (copied from CloudNativePG app secret `pg-app-db` in `infra`):

   ```bash
   ./kubernetes/scripts/sync-jobscraper-db-secret.sh
   ```

   If your infra secret uses a different key than `password`, set `PASSWORD_KEY` (see script header).

5. On the Hetzner node, clone this repository to the path you will use for deploys (same path as `DEPLOY_PATH` below). Configure `git` on the server so `git fetch origin main` works (deploy key or cached credentials).

### GitHub configuration

**Secrets (repository)**

| Secret | Purpose |
|--------|---------|
| `SSH_PRIVATE_KEY` | Private key for SSH from Actions to the Hetzner server (no kubeconfig in GitHub). |

`GITHUB_TOKEN` is provided automatically for GHCR login and push.

**Variables (repository)**

| Variable | Required | Purpose |
|----------|----------|---------|
| `DEPLOY_HOST` | Yes | Server hostname or IP for SSH. |
| `DEPLOY_USER` | Yes | SSH user (must have `kubectl` and a valid kubeconfig for your k3s cluster). |
| `DEPLOY_PATH` | Yes | Absolute path to this repo on the server (e.g. `/home/deploy/job-helper-scraper`). |

Optional: if SSH is not on port 22, add a `port:` line to the `appleboy/ssh-action` step in [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml).

### What CI does

On every push to `main`, [`.github/workflows/deploy.yml`](.github/workflows/deploy.yml):

1. Builds the Docker image from the repo root [`Dockerfile`](Dockerfile).
2. Pushes to GHCR as `ghcr.io/<owner>/<repo>:<sha>` and `:latest` (repository name lowercased for GHCR).
3. SSHs to Hetzner, runs `git fetch` / `git reset --hard origin/main`, then [`kubernetes/harco/deploy.sh`](kubernetes/harco/deploy.sh) with the `:sha` image.
4. Waits for `kubectl rollout status` on `deployment/jobscraper`.

Kubernetes API access stays on the server; GitHub never talks to the cluster API.

### Kubernetes objects (namespace `harco`)

| Object | Name | Role |
|--------|------|------|
| ConfigMap | `jobscraper-config` | `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `PGSSLMODE` |
| Secret | `jobscraper-db` | `DB_PASSWORD` (sync from `infra/pg-app-db`) |
| Deployment | `jobscraper` | Runs `python -u main.py`; `imagePullSecrets: ghcr-pull-secret` |

Exact connectivity env vars:

- `DB_HOST` = `app-postgres-rw.infra.svc.cluster.local`
- `DB_PORT` = `5432`
- `DB_NAME` = `resume_jobs`
- `DB_USER` = `resume_user`
- `DB_PASSWORD` = from secret `jobscraper-db` / key `DB_PASSWORD`
- `PGSSLMODE` = `prefer` (override in ConfigMap if your CNPG setup needs `require` / `verify-full` / `disable`)

### Operations

```bash
kubectl logs -f deployment/jobscraper -n harco
kubectl rollout restart deployment/jobscraper -n harco
```

More detail: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).
