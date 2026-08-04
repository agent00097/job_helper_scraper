-- Mirror of job-helper-infra V006__jobs_geo_normalization.sql
-- Prefer applying via Flyway in job-helper-infra; this copy is for local/dev reference.

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS country_code TEXT,
    ADD COLUMN IF NOT EXISTS admin1_code TEXT,
    ADD COLUMN IF NOT EXISTS admin1_name TEXT,
    ADD COLUMN IF NOT EXISTS locality TEXT,
    ADD COLUMN IF NOT EXISTS geo_precision TEXT;

CREATE INDEX IF NOT EXISTS idx_jobs_country_code
    ON jobs (country_code);

CREATE INDEX IF NOT EXISTS idx_jobs_admin1_code
    ON jobs (admin1_code)
    WHERE admin1_code IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_jobs_locality_admin1
    ON jobs (lower(locality), admin1_code)
    WHERE locality IS NOT NULL;
