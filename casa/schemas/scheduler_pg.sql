-- CASA PgSchedulerBackend 参考 schema

CREATE TABLE IF NOT EXISTS scheduler_sessions (
    session_id TEXT PRIMARY KEY,
    active_slots INT NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS scheduler_runs (
    run_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id TEXT DEFAULT '',
    tenant_id TEXT DEFAULT '',
    intent TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    error TEXT DEFAULT '',
    slots_used INT DEFAULT 1,
    idempotency_key TEXT DEFAULT '',
    created_at TEXT DEFAULT '',
    last_heartbeat DOUBLE PRECISION DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_runs_session_status
    ON scheduler_runs(session_id, status);
CREATE INDEX IF NOT EXISTS idx_runs_idempotency
    ON scheduler_runs(session_id, idempotency_key);
