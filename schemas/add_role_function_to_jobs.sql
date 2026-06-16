-- Add role_function to the jobs table (standalone additive column; no dependency on other migrations).
-- Populated at insert time by utils.role_classifier.classify_role(title, noc_code).
-- Values: one of the role_function taxonomy values (e.g. 'software_engineering'), or NULL until backfilled.

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS role_function TEXT;

CREATE INDEX IF NOT EXISTS idx_jobs_role_function ON jobs(role_function);
