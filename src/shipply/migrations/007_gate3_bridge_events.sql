-- 007: Gate 3 bridge event tracking and PR URL index.
-- Idempotent: safe to run on every handler startup.

CREATE INDEX IF NOT EXISTS idx_molecules_pr_url ON molecules(pr_url);

CREATE TABLE IF NOT EXISTS bridge_events (
    pr_url TEXT PRIMARY KEY,
    last_event_at TEXT,
    last_event_state TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_bridge_events_pr_url ON bridge_events(pr_url);
