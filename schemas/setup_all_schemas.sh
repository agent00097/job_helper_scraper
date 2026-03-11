#!/bin/bash
# Setup all database schemas for job-helper-scraper

# Check if DATABASE_URL is set
if [ -z "$DATABASE_URL" ]; then
    echo "Error: DATABASE_URL environment variable is not set"
    echo "Please set it in your .env file or export it:"
    echo "  export DATABASE_URL='postgresql://user:password@host:port/database'"
    exit 1
fi

echo "Setting up database schemas..."
echo ""

# Run schema files in order
echo "1. Creating job_sources table..."
psql "$DATABASE_URL" -f create_job_sources_table.sql

echo "2. Creating source_credentials table..."
psql "$DATABASE_URL" -f create_source_credentials_table.sql

echo "3. Creating source_companies table..."
psql "$DATABASE_URL" -f create_source_companies_table.sql

echo "4. Adding content_hash to jobs table..."
psql "$DATABASE_URL" -f add_content_hash_to_jobs.sql

echo "5. Setting up Greenhouse source..."
psql "$DATABASE_URL" -f setup_greenhouse_source.sql

echo ""
echo "✅ All schemas created successfully!"
echo ""
echo "Verify setup with:"
echo "  psql \$DATABASE_URL -c \"SELECT * FROM job_sources WHERE name = 'greenhouse';\""
echo "  psql \$DATABASE_URL -c \"SELECT * FROM source_companies WHERE source_id = (SELECT id FROM job_sources WHERE name = 'greenhouse');\""
