-- Capped per-user job match cache + match run state.
-- Keep in sync with job_matcher/schemas/create_user_job_matches_tables.sql

CREATE TABLE IF NOT EXISTS user_job_matches (
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    score REAL NOT NULL,
    skill_score REAL NOT NULL DEFAULT 0,
    reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    matched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, job_id),
    CONSTRAINT user_job_matches_score_check CHECK (score >= 0 AND score <= 1),
    CONSTRAINT user_job_matches_skill_score_check CHECK (skill_score >= 0 AND skill_score <= 1)
);

CREATE INDEX IF NOT EXISTS idx_user_job_matches_user_score
    ON user_job_matches (user_id, score DESC);

CREATE INDEX IF NOT EXISTS idx_user_job_matches_job_id
    ON user_job_matches (job_id);

CREATE TABLE IF NOT EXISTS user_match_state (
    user_id UUID PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    last_matched_at TIMESTAMPTZ,
    last_match_reason TEXT,
    match_cap INT NOT NULL DEFAULT 500,
    scroll_depth INT NOT NULL DEFAULT 0,
    expand_requested BOOLEAN NOT NULL DEFAULT FALSE,
    candidate_window_days INT NOT NULL DEFAULT 60,
    matches_written INT NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
