-- Setup Greenhouse as a job source
-- Greenhouse has public job boards that don't require API keys
-- Companies post jobs at: boards.greenhouse.io/{company-slug}

-- Insert Greenhouse source
INSERT INTO job_sources (name, type, enabled, schedule_hours, rate_limit_per_minute, config)
VALUES (
    'greenhouse',
    'api',
    TRUE,
    6,  -- Run every 6 hours
    60,  -- Conservative rate limit (60 requests per minute)
    '{
        "base_url": "https://boards-api.greenhouse.io/v1",
        "public_boards_url": "https://boards.greenhouse.io",
        "use_public_api": true
    }'::jsonb
)
ON CONFLICT (name) DO UPDATE
SET 
    type = EXCLUDED.type,
    enabled = EXCLUDED.enabled,
    schedule_hours = EXCLUDED.schedule_hours,
    rate_limit_per_minute = EXCLUDED.rate_limit_per_minute,
    config = EXCLUDED.config,
    updated_at = CURRENT_TIMESTAMP;

-- Note: Greenhouse public boards don't require API keys
-- If you need to use Greenhouse's private API (less common), you would add credentials here:
-- INSERT INTO source_credentials (source_id, credential_type, credential_value, is_env_var)
-- SELECT id, 'api_key', 'GREENHOUSE_API_KEY', TRUE
-- FROM job_sources WHERE name = 'greenhouse'
-- ON CONFLICT (source_id, credential_type) DO NOTHING;

-- Add some example companies (you can add more later)
-- These are popular companies that use Greenhouse
INSERT INTO source_companies (source_id, company_name, company_endpoint, enabled)
SELECT 
    id,
    company_name,
    company_endpoint,
    TRUE
FROM job_sources,
(VALUES
    ('Airbnb', 'airbnb'),
    ('Stripe', 'stripe'),
    ('Reddit', 'reddit'),
    ('Pinterest', 'pinterest'),
    ('Shopify', 'shopify'),
    ('GitHub', 'github'),
    ('Dropbox', 'dropbox'),
    ('Khan Academy', 'khanacademy'),
    ('MongoDB', 'mongodb'),
    ('Asana', 'asana')
) AS companies(company_name, company_endpoint)
WHERE job_sources.name = 'greenhouse'
ON CONFLICT (source_id, company_endpoint) DO NOTHING;
