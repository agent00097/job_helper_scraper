-- Add content_hash column to jobs table for deduplication
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS content_hash TEXT;

-- Create index for fast duplicate detection
CREATE INDEX IF NOT EXISTS idx_jobs_content_hash ON jobs(content_hash);

-- Create composite index for company + location + date for fuzzy matching
CREATE INDEX IF NOT EXISTS idx_jobs_company_location_date ON jobs(company, location, date_posted);
