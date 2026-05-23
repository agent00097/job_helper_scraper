-- Setup Workday as a job source
-- Workday CXS API: per-tenant, POST-based list + per-job GET detail.
-- No global base URL — each company_endpoint IS the full board URL.
-- See sources/api/workday_source.py: parse_workday_board_url() extracts
-- (tenant, dc, site) from the board URL at fetch time.

INSERT INTO job_sources (name, type, enabled, schedule_hours, rate_limit_per_minute, config)
VALUES (
    'workday',
    'api',
    TRUE,
    12,  -- Run every 12 hours (each company triggers many detail GETs; be conservative)
    30,  -- 30 requests per minute (each job fetch = 1 list POST + 1 detail GET)
    '{}'::jsonb
)
ON CONFLICT (name) DO UPDATE
SET
    type                  = EXCLUDED.type,
    enabled               = EXCLUDED.enabled,
    schedule_hours        = EXCLUDED.schedule_hours,
    rate_limit_per_minute = EXCLUDED.rate_limit_per_minute,
    config                = EXCLUDED.config,
    updated_at            = CURRENT_TIMESTAMP;

-- Starter company — verified working against the live Workday API.
-- IMPORTANT: Workday company_endpoint is the FULL board URL, not a slug.
-- Format: https://{tenant}.{dc}.myworkdayjobs.com/{site}
INSERT INTO source_companies (source_id, company_name, company_endpoint, enabled)
SELECT
    js.id,
    companies.company_name,
    companies.company_endpoint,
    TRUE
FROM job_sources js,
(VALUES
    ('Salesforce', 'https://salesforce.wd12.myworkdayjobs.com/External_Career_Site')
) AS companies(company_name, company_endpoint)
WHERE js.name = 'workday'
ON CONFLICT (source_id, company_endpoint) DO NOTHING;
