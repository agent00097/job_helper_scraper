-- Setup SmartRecruiters as a job source
-- Public Posting API: https://api.smartrecruiters.com/v1/companies/{companyIdentifier}/postings
-- No API key required for public career-site feeds.
-- company_endpoint is the case-sensitive identifier from
-- https://jobs.smartrecruiters.com/{companyIdentifier}

INSERT INTO job_sources (name, type, enabled, schedule_hours, rate_limit_per_minute, config)
VALUES (
    'smartrecruiters',
    'api',
    TRUE,
    6,   -- Run every 6 hours
    30,  -- Conservative: list is paginated and new jobs need a detail GET
    '{
        "base_url": "https://api.smartrecruiters.com/v1/companies",
        "max_detail_fetches_per_run": 50
    }'::jsonb
)
ON CONFLICT (name) DO UPDATE
SET
    type                  = EXCLUDED.type,
    enabled               = EXCLUDED.enabled,
    schedule_hours        = EXCLUDED.schedule_hours,
    rate_limit_per_minute = EXCLUDED.rate_limit_per_minute,
    config                = EXCLUDED.config,
    updated_at            = CURRENT_TIMESTAMP;

-- Starter company — documented public identifier from SmartRecruiters' own API docs.
INSERT INTO source_companies (source_id, company_name, company_endpoint, enabled)
SELECT
    js.id,
    companies.company_name,
    companies.company_endpoint,
    TRUE
FROM job_sources js,
(VALUES
    ('SmartRecruiters', 'smartrecruiters')
) AS companies(company_name, company_endpoint)
WHERE js.name = 'smartrecruiters'
ON CONFLICT (source_id, company_endpoint) DO NOTHING;
