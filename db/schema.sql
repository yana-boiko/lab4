CREATE TABLE IF NOT EXISTS lab_results (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    patient_code VARCHAR(32)    NOT NULL,
    test_code    VARCHAR(32)    NOT NULL,
    value        NUMERIC(14, 4) NOT NULL,
    unit         VARCHAR(20)    NOT NULL,
    ref_low      NUMERIC(14, 4) NOT NULL,
    ref_high     NUMERIC(14, 4) NOT NULL,
    sample_date  DATE           NOT NULL,
    CONSTRAINT ref_range_valid CHECK (ref_low < ref_high)
);

CREATE INDEX IF NOT EXISTS idx_lab_results_test_code ON lab_results (test_code);

CREATE TABLE IF NOT EXISTS tasks (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    record_id    BIGINT      NOT NULL REFERENCES lab_results (id) ON DELETE CASCADE,
    status       VARCHAR(10) NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'running', 'done', 'failed')),
    seed         BIGINT      NOT NULL,
    attempts     INT         NOT NULL DEFAULT 0,
    max_attempts INT         NOT NULL DEFAULT 3,
    token        BIGINT      NOT NULL DEFAULT 0,
    lease_until  TIMESTAMPTZ,
    worker_id    VARCHAR(64),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at   TIMESTAMPTZ,
    finished_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks (status, created_at);

CREATE TABLE IF NOT EXISTS task_results (
    task_id    BIGINT      PRIMARY KEY REFERENCES tasks (id) ON DELETE CASCADE,
    token      BIGINT      NOT NULL,
    worker_id  VARCHAR(64) NOT NULL,
    result     JSONB       NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS cancel_requested_at TIMESTAMPTZ;
ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_status_check;
ALTER TABLE tasks ADD CONSTRAINT tasks_status_check
    CHECK (status IN ('queued', 'running', 'done', 'failed', 'cancelled'));