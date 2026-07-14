-- 006: Forge molecule and PR URL tracking.
-- Idempotent: safe to run on every handler startup.

CREATE TABLE IF NOT EXISTS molecules (
    proposal_id TEXT PRIMARY KEY,
    molecule_id TEXT NOT NULL,
    pr_url TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (proposal_id) REFERENCES proposals(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_molecules_molecule_id ON molecules(molecule_id);
