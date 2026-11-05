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
