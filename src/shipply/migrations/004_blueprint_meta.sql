-- 004: Blueprint revision metadata.
-- Marks a Blueprint revision as frozen before Gate 2 authorizes FORGE.
-- Stored in a separate table so the migration is idempotent and avoids
-- SQLite ALTER COLUMN limitations.

CREATE TABLE IF NOT EXISTS blueprint_frozen (
    revision_id TEXT PRIMARY KEY,
    frozen_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (revision_id) REFERENCES revisions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_blueprint_frozen_revision_id ON blueprint_frozen(revision_id);
