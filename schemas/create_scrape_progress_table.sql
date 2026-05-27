CREATE TABLE IF NOT EXISTS scrape_progress (
    id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id                UUID NOT NULL REFERENCES job_sources(id) ON DELETE CASCADE,
    last_scraped_at          TIMESTAMPTZ,         -- incremental watermark
    backfill_last_page       INTEGER  DEFAULT 0,  -- last page backfill completed (0 = not started)
    backfill_oldest_job_date DATE,                -- oldest job date_posted the backfill has reached
    backfill_completed_at    TIMESTAMPTZ,         -- NULL until backfill finishes
    updated_at               TIMESTAMPTZ DEFAULT NOW(),
    CONSTRAINT uq_scrape_progress_source UNIQUE (source_id)
);

CREATE INDEX IF NOT EXISTS idx_scrape_progress_source_id ON scrape_progress(source_id);

COMMENT ON TABLE scrape_progress IS
    'Per-source operational state for incremental and backfill runs.';
COMMENT ON COLUMN scrape_progress.last_scraped_at IS
    'Incremental mode: timestamp of the most recent successful run. Used as the freshness watermark.';
COMMENT ON COLUMN scrape_progress.backfill_last_page IS
    'Backfill mode: last page number successfully completed. 0 means backfill has not started.';
COMMENT ON COLUMN scrape_progress.backfill_oldest_job_date IS
    'Backfill mode: oldest job date_posted seen so far (tracks how far back we have gone).';
COMMENT ON COLUMN scrape_progress.backfill_completed_at IS
    'Backfill mode: set to NOW() when the last page of results is reached. NULL while in progress.';
