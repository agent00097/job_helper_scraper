-- Insert Job Bank source (disabled until backfill is complete — flip enabled=TRUE manually)
INSERT INTO job_sources (name, type, enabled, schedule_hours, rate_limit_per_minute, config)
VALUES (
    'jobbank',
    'scraper',
    FALSE,
    24,
    30,
    '{}'::jsonb
)
ON CONFLICT (name) DO UPDATE
    SET type                  = EXCLUDED.type,
        schedule_hours        = EXCLUDED.schedule_hours,
        rate_limit_per_minute = EXCLUDED.rate_limit_per_minute,
        updated_at            = CURRENT_TIMESTAMP;

-- Single source_companies row: represents the full Job Bank search (no company filter)
INSERT INTO source_companies (source_id, company_name, company_endpoint, enabled)
SELECT
    id,
    'Job Bank (Canada)',
    '',          -- empty: no query-string fragment; fetches all recent postings
    TRUE
FROM job_sources
WHERE name = 'jobbank'
ON CONFLICT (source_id, company_endpoint) DO NOTHING;

-- Initialise scrape_progress row (backfill starts at page 0 = not started)
INSERT INTO scrape_progress (source_id, backfill_last_page, updated_at)
SELECT id, 0, NOW()
FROM job_sources
WHERE name = 'jobbank'
ON CONFLICT (source_id) DO NOTHING;
