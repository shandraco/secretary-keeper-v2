-- Job queue: runs survive restarts. Workers claim with FOR UPDATE SKIP LOCKED and hold a lease
-- (locked_until) that they renew while working; a crashed worker's job is reclaimed when it expires.
CREATE TABLE IF NOT EXISTS jobs (
  id            bigserial PRIMARY KEY,
  kind          text NOT NULL,
  payload       jsonb NOT NULL DEFAULT '{}',
  status        text NOT NULL DEFAULT 'pending',   -- pending | running | done | dead
  attempts      int  NOT NULL DEFAULT 0,
  max_attempts  int  NOT NULL DEFAULT 3,
  run_at        timestamptz NOT NULL DEFAULT now(),
  locked_by     text,
  locked_until  timestamptz,
  last_error    text,
  dedupe_key    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz
);
CREATE INDEX IF NOT EXISTS jobs_due_idx ON jobs (run_at) WHERE status IN ('pending', 'running');
-- At most one live job per dedupe key (e.g. the periodic review sync).
CREATE UNIQUE INDEX IF NOT EXISTS jobs_dedupe_idx ON jobs (dedupe_key)
  WHERE dedupe_key IS NOT NULL AND status IN ('pending', 'running');

-- Review queue and training data: the exact prompt the model saw, and the reviewer's correction.
ALTER TABLE judgements
  ADD COLUMN IF NOT EXISTS prompt               text,
  ADD COLUMN IF NOT EXISTS reviewer_impact_type text,
  ADD COLUMN IF NOT EXISTS reviewer_note        text,
  ADD COLUMN IF NOT EXISTS review_pushed_at     timestamptz;
CREATE INDEX IF NOT EXISTS judgements_unpushed_idx ON judgements (id) WHERE review_pushed_at IS NULL;

-- runs.status gains 'queued' (waiting for a worker).
ALTER TABLE runs ALTER COLUMN status SET DEFAULT 'queued';
