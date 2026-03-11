-- Create job_sources table for storing job source configurations
CREATE TABLE IF NOT EXISTS job_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT UNIQUE NOT NULL,  -- 'greenhouse', 'ashby', 'lever', 'indeed', etc.
    type TEXT NOT NULL CHECK (type IN ('api', 'scraper')),  -- 'api' or 'scraper'
    enabled BOOLEAN DEFAULT TRUE,
    schedule_hours INTEGER DEFAULT 6,  -- How often to run (in hours)
    rate_limit_per_minute INTEGER,  -- API rate limit (null for scrapers)
    config JSONB,  -- Source-specific configuration (flexible JSON)
    last_run_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_job_sources_enabled ON job_sources(enabled);
CREATE INDEX IF NOT EXISTS idx_job_sources_type ON job_sources(type);
CREATE INDEX IF NOT EXISTS idx_job_sources_name ON job_sources(name);
