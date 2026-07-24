-- Canonical skill dictionary + aliases (seeded from O*NET Software Skills, etc.)

CREATE TABLE IF NOT EXISTS skills (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    category TEXT NULL,
    source TEXT NOT NULL DEFAULT 'onet_software_skills',
    is_hot BOOLEAN NOT NULL DEFAULT FALSE,
    is_in_demand BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT skills_normalized_name_key UNIQUE (normalized_name)
);

CREATE INDEX IF NOT EXISTS idx_skills_category ON skills (category);
CREATE INDEX IF NOT EXISTS idx_skills_source ON skills (source);

CREATE TABLE IF NOT EXISTS skill_aliases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    skill_id UUID NOT NULL REFERENCES skills (id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT skill_aliases_normalized_alias_key UNIQUE (normalized_alias)
);

CREATE INDEX IF NOT EXISTS idx_skill_aliases_skill_id ON skill_aliases (skill_id);
