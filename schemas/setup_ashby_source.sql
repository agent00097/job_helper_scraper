-- Setup Ashby as a job source
-- Ashby public posting API: https://api.ashbyhq.com/posting-api/job-board/{company}
-- No API key required for public boards.

INSERT INTO job_sources (name, type, enabled, schedule_hours, rate_limit_per_minute, config)
VALUES (
    'ashby',
    'api',
    TRUE,
    6,   -- Run every 6 hours
    60,  -- 60 requests per minute (conservative; public API has no published limit)
    '{
        "base_url": "https://api.ashbyhq.com/posting-api/job-board"
    }'::jsonb
)
ON CONFLICT (name) DO UPDATE
SET
    type                 = EXCLUDED.type,
    enabled              = EXCLUDED.enabled,
    schedule_hours       = EXCLUDED.schedule_hours,
    rate_limit_per_minute = EXCLUDED.rate_limit_per_minute,
    config               = EXCLUDED.config,
    updated_at           = CURRENT_TIMESTAMP;

-- Starter company — verified working against the live Ashby API.
INSERT INTO source_companies (source_id, company_name, company_endpoint, enabled)
SELECT
    js.id,
    companies.company_name,
    companies.company_endpoint,
    TRUE
FROM job_sources js,
(VALUES
    ('Linear', 'linear')
) AS companies(company_name, company_endpoint)
WHERE js.name = 'ashby'
ON CONFLICT (source_id, company_endpoint) DO NOTHING;
