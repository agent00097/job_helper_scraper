-- Create source_companies table for tracking companies per source
CREATE TABLE IF NOT EXISTS source_companies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES job_sources(id) ON DELETE CASCADE,
    company_name TEXT NOT NULL,  -- e.g., 'Airbnb', 'Stripe'
    company_endpoint TEXT NOT NULL,  -- e.g., 'airbnb' for boards.greenhouse.io/airbnb
    enabled BOOLEAN DEFAULT TRUE,
    last_fetched_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, company_endpoint)  -- One endpoint per company per source
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_source_companies_source_id ON source_companies(source_id);
CREATE INDEX IF NOT EXISTS idx_source_companies_enabled ON source_companies(enabled);
CREATE INDEX IF NOT EXISTS idx_source_companies_source_enabled ON source_companies(source_id, enabled);
