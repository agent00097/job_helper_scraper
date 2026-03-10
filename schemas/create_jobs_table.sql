-- Create jobs table for storing scraped job information
CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    url TEXT NOT NULL UNIQUE,
    job_title TEXT,
    company TEXT,
    location TEXT,
    job_description TEXT,
    date_posted DATE,
    employment_type TEXT, -- e.g., 'full-time', 'part-time', 'contract', 'internship'
    salary_range TEXT,
    experience_level TEXT, -- e.g., 'entry', 'mid', 'senior'
    education_required TEXT,
    skills_required TEXT[], -- Array of skills
    application_url TEXT,
    sponsorship_required BOOLEAN DEFAULT FALSE,
    citizenship_required BOOLEAN DEFAULT FALSE,
    remote_allowed BOOLEAN DEFAULT FALSE,
    hybrid_allowed BOOLEAN DEFAULT FALSE,
    source_website TEXT NOT NULL, -- e.g., 'LinkedIn', 'Indeed', 'Glassdoor'
    job_id_from_source TEXT, -- External job ID from the source website
    status TEXT DEFAULT 'active', -- 'active', 'expired', 'filled', 'removed'
    last_updated TIMESTAMP, -- When the job was last updated on the source
    scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes for common queries
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
CREATE INDEX IF NOT EXISTS idx_jobs_source_website ON jobs(source_website);
CREATE INDEX IF NOT EXISTS idx_jobs_date_posted ON jobs(date_posted);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_sponsorship_required ON jobs(sponsorship_required);
CREATE INDEX IF NOT EXISTS idx_jobs_citizenship_required ON jobs(citizenship_required);
CREATE INDEX IF NOT EXISTS idx_jobs_scraped_at ON jobs(scraped_at);

-- Create a composite index for source website and job_id_from_source to help detect duplicates
CREATE INDEX IF NOT EXISTS idx_jobs_source_job_id ON jobs(source_website, job_id_from_source);
