-- 003: Reconciled MLS Squad roster cache for quorum calculations.
-- Idempotent: safe to run on every handler startup.

CREATE TABLE IF NOT EXISTS squad_members (
    group_id TEXT NOT NULL,
    member_pubkey TEXT NOT NULL,
    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (group_id, member_pubkey)
);

CREATE INDEX IF NOT EXISTS idx_squad_members_group_id ON squad_members(group_id);
