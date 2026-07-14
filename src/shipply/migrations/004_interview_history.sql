-- 004: Interview history and thread tracking for the Scout handler.
-- Idempotent: safe to run on every handler startup.

CREATE TABLE IF NOT EXISTS interview_history (
    message_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL,
    source TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    author TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (proposal_id) REFERENCES proposals(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_interview_history_proposal_id ON interview_history(proposal_id);
CREATE INDEX IF NOT EXISTS idx_interview_history_thread ON interview_history(source, chat_id);
