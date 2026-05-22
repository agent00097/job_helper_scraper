-- Phase 2: add country_code to the jobs table.
-- Populated at insert time by utils.geo.derive_country(location).
-- Values: 'CA' (Canada), 'US' (United States), NULL (indeterminate).

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS country_code TEXT;

CREATE INDEX IF NOT EXISTS idx_jobs_country_code ON jobs(country_code);
