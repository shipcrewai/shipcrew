-- 002: Observability views over the proposal state tables.
-- Views are dropped and recreated so schema changes are applied on re-run.

DROP VIEW IF EXISTS v_proposals_in_flight;
CREATE VIEW IF NOT EXISTS v_proposals_in_flight AS
SELECT
    state AS stage,
    COUNT(*) AS count
FROM proposals
WHERE state != 'CLOSED'
GROUP BY state;

DROP VIEW IF EXISTS v_beads_in_flight;
CREATE VIEW IF NOT EXISTS v_beads_in_flight AS
SELECT
    status,
    COUNT(*) AS count
FROM beads
GROUP BY status;

DROP VIEW IF EXISTS v_stage_latency;
CREATE VIEW IF NOT EXISTS v_stage_latency AS
SELECT
    proposal_id,
    stage,
    MIN(created_at) AS entered_at,
    MAX(created_at) AS last_event_at
FROM events
WHERE stage IS NOT NULL
GROUP BY proposal_id, stage;
