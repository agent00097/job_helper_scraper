-- Junction tables: jobs/users ↔ skills (weights from alias/embedding extraction)

CREATE TABLE IF NOT EXISTS job_skills (
    job_id UUID NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    skill_id UUID NOT NULL REFERENCES skills (id) ON DELETE CASCADE,
    weight REAL NOT NULL,
    method TEXT NOT NULL DEFAULT 'alias',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, skill_id),
    CONSTRAINT job_skills_weight_check CHECK (weight > 0 AND weight <= 1)
);

CREATE INDEX IF NOT EXISTS idx_job_skills_skill_id ON job_skills (skill_id);
CREATE INDEX IF NOT EXISTS idx_job_skills_job_id ON job_skills (job_id);

CREATE TABLE IF NOT EXISTS user_skills (
    user_id UUID NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    skill_id UUID NOT NULL REFERENCES skills (id) ON DELETE CASCADE,
    weight REAL NOT NULL,
    method TEXT NOT NULL DEFAULT 'profile',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, skill_id),
    CONSTRAINT user_skills_weight_check CHECK (weight > 0 AND weight <= 1)
);

CREATE INDEX IF NOT EXISTS idx_user_skills_skill_id ON user_skills (skill_id);
CREATE INDEX IF NOT EXISTS idx_user_skills_user_id ON user_skills (user_id);
