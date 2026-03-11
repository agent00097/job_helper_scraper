-- Create source_credentials table for storing API keys and authentication
CREATE TABLE IF NOT EXISTS source_credentials (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES job_sources(id) ON DELETE CASCADE,
    credential_type TEXT NOT NULL,  -- 'api_key', 'endpoint', 'username', 'password', etc.
    credential_value TEXT,  -- Can store env var name like 'GREENHOUSE_API_KEY' or actual value
    is_env_var BOOLEAN DEFAULT FALSE,  -- If true, credential_value is an env var name
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(source_id, credential_type)  -- One credential type per source
);

-- Create indexes
CREATE INDEX IF NOT EXISTS idx_source_credentials_source_id ON source_credentials(source_id);
