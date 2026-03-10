-- Create job_keywords table for storing keywords by job category
-- This table supports multiple job categories (tech, finance, healthcare, etc.)
CREATE TABLE IF NOT EXISTS job_keywords (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    category TEXT NOT NULL, -- e.g., 'tech', 'finance', 'healthcare', 'marketing'
    keyword TEXT NOT NULL,  -- The actual keyword to search for
    active BOOLEAN DEFAULT TRUE, -- Enable/disable this keyword
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(category, keyword) -- Prevent duplicate keywords in same category
);

-- Create indexes for efficient queries
CREATE INDEX IF NOT EXISTS idx_job_keywords_category ON job_keywords(category);
CREATE INDEX IF NOT EXISTS idx_job_keywords_active ON job_keywords(active);
CREATE INDEX IF NOT EXISTS idx_job_keywords_category_active ON job_keywords(category, active);

-- Insert initial tech keywords (you can modify these as needed)
INSERT INTO job_keywords (category, keyword) VALUES
    ('tech', 'software engineer'),
    ('tech', 'developer'),
    ('tech', 'programmer'),
    ('tech', 'data scientist'),
    ('tech', 'software developer'),
    ('tech', 'tech'),
    ('tech', 'IT'),
    ('tech', 'computer science')
ON CONFLICT (category, keyword) DO NOTHING; -- Don't insert if already exists
