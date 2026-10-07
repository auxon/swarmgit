-- SwarmGit board state on D1. Apply with:
--   wrangler d1 execute swarmgit --remote --file=src/schema.sql
-- (local dev: wrangler d1 execute swarmgit --local --file=src/schema.sql)
--
-- Forked from the TestSwarm worker scaffold; all tables below are
-- SwarmGit-specific. New tables only: coding tasks, claims, forks,
-- verifications, merges, dry-run escrow ledger, reputation, why-graph
-- staging, plus the inherited idempotency and post_rate patterns.

CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,          -- open | claimed | working | submitted |
                                 -- verifying | merging | settled |
                                 -- expired | disputed
  repo TEXT NOT NULL,            -- Artifacts repo backing the task
  bounty_sats INTEGER NOT NULL,
  poster TEXT NOT NULL DEFAULT 'anon',
  data TEXT NOT NULL,            -- full task JSON (spec, acceptance tests)
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sgtasks_status ON tasks(status);

CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  agent TEXT NOT NULL,
  status TEXT NOT NULL,          -- active | submitted | won | lost |
                                 -- gate_blocked
  data TEXT NOT NULL,            -- full claim JSON
  created_at INTEGER NOT NULL,
  UNIQUE(task_id, agent)         -- one claim per agent per task: no double-work
);
CREATE INDEX IF NOT EXISTS idx_sgclaims_task ON claims(task_id);

CREATE TABLE IF NOT EXISTS forks (
  fork_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  claim_id TEXT NOT NULL,
  repo_name TEXT NOT NULL,       -- Artifacts fork repo name
  preview_url TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,          -- working | submitted | verifying |
                                 -- gate_blocked | merged | rejected
  data TEXT NOT NULL,            -- full fork JSON
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sgforks_task ON forks(task_id);
CREATE INDEX IF NOT EXISTS idx_sgforks_repo ON forks(repo_name);

CREATE TABLE IF NOT EXISTS verifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  fork_id TEXT NOT NULL,
  verifier TEXT NOT NULL,
  verdict TEXT NOT NULL,         -- pass | fail
  stake_sats INTEGER NOT NULL,
  repro TEXT NOT NULL DEFAULT '',-- required when verdict = fail
  data TEXT NOT NULL,            -- full verification JSON
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sgverifs_fork ON verifications(fork_id);

CREATE TABLE IF NOT EXISTS merges (
  merge_id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL,
  fork_id TEXT NOT NULL,
  decision TEXT NOT NULL,        -- merged | gate_blocked
  audit_report TEXT NOT NULL,    -- merge-gate report JSON
  why_refs TEXT NOT NULL,        -- JSON list of why-entry ids
  data TEXT NOT NULL,            -- full merge JSON
  merged_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sgmerges_task ON merges(task_id);

-- Dry-run escrow ledger. settlement is ALWAYS "dry_run" until Richard's
-- POST /agent/send primitive exists (see README "Flipping settlement live").
CREATE TABLE IF NOT EXISTS escrow_ledger (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  t INTEGER NOT NULL,
  kind TEXT NOT NULL,            -- escrow_lock | escrow_release |
                                 -- payout | stake_lock | stake_release
  task_id TEXT NOT NULL,
  agent TEXT NOT NULL DEFAULT '',
  sats INTEGER NOT NULL,
  settlement TEXT NOT NULL,      -- "dry_run" (only value until live)
  detail TEXT NOT NULL           -- JSON
);
CREATE INDEX IF NOT EXISTS idx_sgescrow_task ON escrow_ledger(task_id);

CREATE TABLE IF NOT EXISTS reputation (
  agent TEXT PRIMARY KEY,
  score INTEGER NOT NULL DEFAULT 0,
  tasks_won INTEGER NOT NULL DEFAULT 0,
  tasks_verified INTEGER NOT NULL DEFAULT 0,
  false_reports INTEGER NOT NULL DEFAULT 0
);

-- Staged why-graph entries: signed rationale records for each merge.
-- The worker stages them here; the operator exports them to the
-- agent-memory layer with tools/export_why.py (the worker itself never
-- posts to any board).
CREATE TABLE IF NOT EXISTS why_entries (
  id TEXT PRIMARY KEY,
  merge_id TEXT NOT NULL,
  kind TEXT NOT NULL,            -- task_posted | attempt | verification |
                                 -- rationale | decision
  entry TEXT NOT NULL,           -- JSON (text, agent, refs, topic)
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sgwhy_merge ON why_entries(merge_id);

CREATE TABLE IF NOT EXISTS idempotency (
  key TEXT PRIMARY KEY,
  tool TEXT NOT NULL,
  result TEXT NOT NULL,          -- JSON
  fingerprint TEXT,              -- "" when none (None can't be bound:
                                 -- the Python FFI turns it into undefined,
                                 -- which D1 rejects)
  at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS post_rate (
  host TEXT NOT NULL,
  at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sgpost_rate_host ON post_rate(host, at);

CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  sub TEXT NOT NULL,
  email TEXT NOT NULL,
  name TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sgsessions_exp ON sessions(expires_at);

-- Disputed arbitration: a contested verification outcome freezes the
-- fork (status disputed) until Clef rules (see arbiter.py) or a human
-- operator decides on low confidence.
CREATE TABLE IF NOT EXISTS disputes (
  dispute_id TEXT PRIMARY KEY,
  fork_id TEXT NOT NULL,
  task_id TEXT NOT NULL,
  disputer TEXT NOT NULL,
  grounds TEXT NOT NULL,
  repro TEXT NOT NULL,
  contested TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  ruling TEXT NOT NULL DEFAULT '',
  decided_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sgdisputes_fork ON disputes(fork_id);
CREATE INDEX IF NOT EXISTS idx_sgdisputes_task ON disputes(task_id);
