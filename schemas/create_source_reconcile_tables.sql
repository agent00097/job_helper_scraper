-- Broken-board reconciliation: a reviewed plan for every failing (company, source).
--
-- source_reconcile_runs     one row per "Find broken boards" run (admin panel)
-- source_reconcile_actions  one proposed change per (company, source); applied
--                           only when an admin approves it. Stores the company's
--                           previous endpoints/enabled flag so it can be undone.
--
-- Nothing in these tables changes source_companies by itself.

CREATE TABLE IF NOT EXISTS source_reconcile_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'failed')),
    requested_by TEXT,                       -- admin email/id from the backend
    include_empty_boards BOOLEAN NOT NULL DEFAULT FALSE,
    summary JSONB,                           -- counts per action/confidence
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS source_reconcile_actions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES source_reconcile_runs(id) ON DELETE CASCADE,
    company_id UUID NOT NULL REFERENCES source_companies(id) ON DELETE CASCADE,
    action TEXT NOT NULL
        CHECK (action IN ('wait', 'remove_source', 'relocate', 'disable_company', 'review', 'keep')),
    confidence TEXT NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    source TEXT NOT NULL,                    -- the failing/empty source key
    endpoint TEXT NOT NULL,                  -- its endpoint when the plan was made
    new_source TEXT,                         -- relocate only
    new_endpoint TEXT,
    new_board_jobs INTEGER,
    category TEXT NOT NULL,                  -- board_gone, blocked, empty_board, duplicate_row, transient, ...
    reason TEXT NOT NULL,                    -- short failure label
    evidence TEXT,                           -- why this action was chosen
    status TEXT NOT NULL DEFAULT 'proposed'
        CHECK (status IN ('proposed', 'applied', 'skipped', 'failed', 'undone')),
    status_note TEXT,                        -- e.g. "board answered on re-check, not applied"
    previous_endpoints JSONB,                -- snapshot taken when applied (for undo)
    previous_enabled BOOLEAN,
    applied_by TEXT,
    applied_at TIMESTAMPTZ,
    undone_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_source_reconcile_actions_run
    ON source_reconcile_actions (run_id, action, confidence);
CREATE INDEX IF NOT EXISTS idx_source_reconcile_actions_company
    ON source_reconcile_actions (company_id);
-- Building a plan scans recent results per (company, source):
CREATE INDEX IF NOT EXISTS idx_scrape_company_results_company_source_finished
    ON scrape_company_results (company_id, source_name, finished_at DESC);
