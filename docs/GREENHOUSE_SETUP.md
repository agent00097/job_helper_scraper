# Greenhouse Setup Guide

## How Greenhouse Works

Greenhouse has **two ways** to access job listings:

### 1. **Public Job Boards** (Most Common - No API Key Needed)
- Companies post jobs at: `https://boards.greenhouse.io/{company-slug}`
- Example: `https://boards.greenhouse.io/airbnb`
- These are **publicly accessible** - no authentication required
- We can fetch jobs using their public API endpoint: `https://boards-api.greenhouse.io/v1/boards/{company-slug}/jobs`

### 2. **Private Greenhouse API** (Less Common - Requires API Key)
- Some companies have private Greenhouse APIs
- Requires API key from Greenhouse
- Usually only needed if you're a Greenhouse customer

## For This Project

We'll use **Option 1 (Public API)** - no API key needed!

The Greenhouse public API endpoint format:
```
GET https://boards-api.greenhouse.io/v1/boards/{company-slug}/jobs
```

Example:
```bash
curl https://boards-api.greenhouse.io/v1/boards/airbnb/jobs
```

## Setup Steps

1. **Run the database schemas:**
   ```bash
   psql $DATABASE_URL -f schemas/create_job_sources_table.sql
   psql $DATABASE_URL -f schemas/create_source_credentials_table.sql
   psql $DATABASE_URL -f schemas/create_source_companies_table.sql
   psql $DATABASE_URL -f schemas/add_content_hash_to_jobs.sql
   psql $DATABASE_URL -f schemas/setup_greenhouse_source.sql
   ```

2. **Verify setup:**
   ```sql
   -- Check source was created
   SELECT * FROM job_sources WHERE name = 'greenhouse';
   
   -- Check companies were added
   SELECT * FROM source_companies WHERE source_id = (SELECT id FROM job_sources WHERE name = 'greenhouse');
   ```

3. **Test API manually:**
   ```bash
   curl https://boards-api.greenhouse.io/v1/boards/airbnb/jobs | jq
   ```

## Adding More Companies

To add more companies, insert into `source_companies`:
```sql
INSERT INTO source_companies (source_id, company_name, company_endpoint, enabled)
SELECT id, 'Company Name', 'company-slug', TRUE
FROM job_sources WHERE name = 'greenhouse';
```

## Rate Limits

Greenhouse doesn't publish official rate limits for public boards, but:
- We set a conservative limit: **60 requests/minute**
- This should be safe and avoid getting blocked
- Can adjust in `job_sources.rate_limit_per_minute` if needed
