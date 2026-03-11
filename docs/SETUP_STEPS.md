# Step-by-Step Setup Guide

## Step 1: Create Database Schemas

Run all schema files in order:

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

## Step 2: Verify Setup

Check that Greenhouse source was created:
```sql
SELECT * FROM job_sources WHERE name = 'greenhouse';
```

Check that companies were added:
```sql
SELECT * FROM source_companies 
WHERE source_id = (SELECT id FROM job_sources WHERE name = 'greenhouse');
```

## Step 3: Test Greenhouse API (No API Key Needed!)

Greenhouse uses **public job boards** - no API key required!

Test manually:
```bash
# Test Airbnb's job board
curl https://boards-api.greenhouse.io/v1/boards/airbnb/jobs | jq

# Test Stripe's job board
curl https://boards-api.greenhouse.io/v1/boards/stripe/jobs | jq
```

## Step 4: What's Next?

After schemas are set up, we'll create:
1. **Base source class** - Abstract interface for all sources
2. **Greenhouse API source** - Fetches jobs from Greenhouse
3. **Deduplication logic** - Prevents duplicate jobs
4. **Worker system** - Runs sources periodically
5. **Scheduler** - Manages all workers

## About Greenhouse API

- **No API Key Required** - Public boards are free to access
- **Rate Limit**: We set 60 requests/minute (conservative)
- **Endpoint Format**: `https://boards-api.greenhouse.io/v1/boards/{company-slug}/jobs`
- **Response**: JSON with job listings

## Adding More Companies

To add more Greenhouse companies:
```sql
INSERT INTO source_companies (source_id, company_name, company_endpoint, enabled)
SELECT id, 'Your Company', 'company-slug', TRUE
FROM job_sources WHERE name = 'greenhouse';
```

Find company slugs by visiting: `https://boards.greenhouse.io/{company-slug}`
