-- Schema for secretary-keeper. Runs once, when the database volume is first created.
-- To change the embedding model's dimension later, see docs/operations.md ("Changing the embedding model").

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE policies (
  id         text PRIMARY KEY,
  title      text NOT NULL,
  owner      text,
  parent_id  text                      -- no foreign key: parents may be ingested after children
);

CREATE TABLE policy_versions (
  policy_id   text NOT NULL REFERENCES policies(id) ON DELETE CASCADE,
  version     int  NOT NULL,
  body        text NOT NULL,
  ingested_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (policy_id, version)
);

CREATE TABLE clauses (
  policy_id  text NOT NULL,
  version    int  NOT NULL,
  number     text NOT NULL,            -- '4.2'; '0' is text before the first numbered section
  heading    text NOT NULL DEFAULT '',
  body       text NOT NULL,
  embedding  vector(768),              -- must equal the embedding model's output size (EMBED_DIM in .env)
  PRIMARY KEY (policy_id, version, number),
  FOREIGN KEY (policy_id, version) REFERENCES policy_versions ON DELETE CASCADE
);
CREATE INDEX clauses_embedding_idx ON clauses USING hnsw (embedding vector_cosine_ops);

CREATE VIEW current_clauses AS
  SELECT c.* FROM clauses c
  JOIN (SELECT policy_id, max(version) AS version FROM policy_versions GROUP BY policy_id) latest
    USING (policy_id, version);

-- The reference graph for each policy's latest version. Replaced whenever that policy is ingested.
CREATE TABLE refs (
  from_policy  text NOT NULL REFERENCES policies(id) ON DELETE CASCADE,
  from_number  text NOT NULL,
  to_policy    text NOT NULL,
  to_number    text,                   -- NULL: cites the whole policy
  source       text NOT NULL DEFAULT 'regex'   -- 'regex' | 'llm'
);
CREATE INDEX refs_to_idx ON refs (to_policy);

CREATE TABLE subsets (
  name       text NOT NULL,
  policy_id  text NOT NULL REFERENCES policies(id) ON DELETE CASCADE,
  PRIMARY KEY (name, policy_id)
);

CREATE TABLE uploads (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  filename    text NOT NULL,
  body        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE runs (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  head_policy   text NOT NULL,
  subset        text NOT NULL,
  upload_id     uuid REFERENCES uploads(id),
  requested_by  text,
  status        text NOT NULL DEFAULT 'running',   -- running | done | failed
  progress      text,
  error         text,
  result        jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz
);

CREATE TABLE changes (
  id          serial PRIMARY KEY,
  run_id      uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  number      text NOT NULL,
  old_number  text,
  kind        text NOT NULL,           -- modified | added | removed | renumbered
  heading     text,
  old_text    text,
  new_text    text,
  terms       text[]
);

-- Audit trail: one row per model decision, including escalations.
CREATE TABLE judgements (
  id                 bigserial PRIMARY KEY,
  run_id             uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  change_id          int  NOT NULL REFERENCES changes(id) ON DELETE CASCADE,
  candidate_policy   text NOT NULL,
  candidate_number   text NOT NULL,
  found_by           text[] NOT NULL,
  stage              text NOT NULL,    -- judge | escalate
  model              text NOT NULL,
  prompt_hash        text NOT NULL,
  result             jsonb NOT NULL,
  created_at         timestamptz NOT NULL DEFAULT now(),
  reviewer_decision  text,
  reviewed_by        text,
  reviewed_at        timestamptz
);
CREATE INDEX judgements_run_idx ON judgements (run_id);
