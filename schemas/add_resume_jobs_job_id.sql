-- Same as resume_tailor/docs/migrations/002_resume_jobs_job_id.sql
-- Run against the shared Postgres database (e.g. resume_jobs).

ALTER TABLE resume_jobs ADD COLUMN IF NOT EXISTS job_id UUID;

CREATE INDEX IF NOT EXISTS idx_resume_jobs_job_id ON resume_jobs(job_id)
    WHERE job_id IS NOT NULL;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'jobs'
    ) THEN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'resume_jobs_job_id_fkey'
        ) THEN
            ALTER TABLE resume_jobs
                ADD CONSTRAINT resume_jobs_job_id_fkey
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE SET NULL;
        END IF;
    END IF;
END $$;
