BEGIN;
CREATE SCHEMA IF NOT EXISTS enterprise_agent;

CREATE TABLE IF NOT EXISTS enterprise_agent.threads (
    thread_id uuid PRIMARY KEY,
    app_id text NOT NULL,
    owner_id text NOT NULL,
    role text NOT NULL CHECK (role IN ('analyst', 'viewer')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS agent_thread_owner_idx
    ON enterprise_agent.threads (app_id, owner_id, created_at DESC);

CREATE TABLE IF NOT EXISTS enterprise_agent.runs (
    thread_id uuid NOT NULL REFERENCES enterprise_agent.threads (thread_id),
    run_id uuid NOT NULL,
    state_version integer NOT NULL,
    task jsonb NOT NULL,
    retry_enabled boolean NOT NULL,
    model_calls integer NOT NULL DEFAULT 0,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    report jsonb,
    PRIMARY KEY (thread_id, run_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS agent_single_active_run
    ON enterprise_agent.runs (thread_id) WHERE report IS NULL;

CREATE TABLE IF NOT EXISTS enterprise_agent.attempts (
    thread_id uuid NOT NULL,
    run_id uuid NOT NULL,
    execution_key text NOT NULL,
    attempt smallint NOT NULL CHECK (attempt BETWEEN 1 AND 2),
    call_id text NOT NULL,
    tool text NOT NULL,
    audit jsonb,
    evidence jsonb,
    PRIMARY KEY (thread_id, run_id, execution_key, attempt),
    FOREIGN KEY (thread_id, run_id) REFERENCES enterprise_agent.runs (thread_id, run_id)
);

-- PostgresSaver is initialized by the app in this schema using a fixed search_path.
GRANT USAGE, CREATE ON SCHEMA enterprise_agent TO rag_app;
GRANT SELECT, INSERT ON enterprise_agent.threads, enterprise_agent.runs, enterprise_agent.attempts TO rag_app;
GRANT UPDATE (report, model_calls) ON enterprise_agent.runs TO rag_app;
GRANT UPDATE (audit, evidence) ON enterprise_agent.attempts TO rag_app;
COMMIT;
