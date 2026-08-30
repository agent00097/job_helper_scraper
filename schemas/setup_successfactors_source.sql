-- Setup SuccessFactors as a job source (RMK career sites).
-- company_endpoint is the career-site origin, NOT a slug:
--   https://jobs.sap.com
-- List: GET {origin}/tile-search-results/?startrow={n}
-- Live source_companies is one row per company with source_endpoints JSONB.

INSERT INTO job_sources (name, type, enabled, schedule_hours, rate_limit_per_minute, config)
VALUES (
    'successfactors',
    'api',
    TRUE,
    12,
    60,
    '{
        "max_detail_fetches_per_run": 50,
        "detail_workers": 4
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

INSERT INTO source_companies (company_name, normalized_name, source_endpoints, enabled)
VALUES (
    'SAP',
    'sap',
    jsonb_build_object('successfactors', 'https://jobs.sap.com'),
    TRUE
)
ON CONFLICT (normalized_name) DO UPDATE SET
    source_endpoints = source_companies.source_endpoints
                       || jsonb_build_object('successfactors', 'https://jobs.sap.com'),
    updated_at       = NOW();
