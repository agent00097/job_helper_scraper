-- Bump default / existing match caps to 500 (safe to re-run).
ALTER TABLE user_match_state
  ALTER COLUMN match_cap SET DEFAULT 500;

UPDATE user_match_state
SET match_cap = 500,
    updated_at = now()
WHERE match_cap < 500;
