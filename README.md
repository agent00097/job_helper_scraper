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
psql $DATabase_URL -f schemas/create_source_companies_table.sql
psql $DATABASE_URL -f schemas/add_content_hash_to_jobs.sql
psql $DATABASE_URL -f schemas/setup_greenhouse_source.sql
```

### 3. Environment Variables

Create a `.env` file:

```bash
DATABASE_URL=postgresql://user:password@host:port/database
```

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
