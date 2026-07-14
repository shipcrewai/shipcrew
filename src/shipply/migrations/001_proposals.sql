-- 001: Initial proposal persistence schema.
-- Idempotent: safe to run on every handler startup.

CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    title TEXT NOT NULL,
    sponsor TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_proposals_state ON proposals(state);

CREATE TABLE IF NOT EXISTS revisions (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    rev_num INTEGER NOT NULL,
    requirements_doc TEXT,
    blueprint_doc TEXT,
    frozen_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (proposal_id) REFERENCES proposals(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_revisions_proposal_id ON revisions(proposal_id);

CREATE TABLE IF NOT EXISTS beads (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    bead_id TEXT NOT NULL,
    status TEXT NOT NULL,
    molecule_id TEXT,
    FOREIGN KEY (proposal_id) REFERENCES proposals(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_beads_proposal_id ON beads(proposal_id);
CREATE INDEX IF NOT EXISTS idx_beads_status ON beads(status);

CREATE TABLE IF NOT EXISTS gates (
    id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    gate_type TEXT NOT NULL,
    status TEXT NOT NULL,
    votes TEXT NOT NULL DEFAULT '{}',
    metadata TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY (proposal_id) REFERENCES proposals(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_gates_proposal_id ON gates(proposal_id);
CREATE INDEX IF NOT EXISTS idx_gates_gate_type ON gates(gate_type);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    stage TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_proposal_id ON events(proposal_id);
CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at);
