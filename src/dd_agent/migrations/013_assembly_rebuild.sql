-- Staged recomputation only: no mutation of assembly_batches, checkpoints or vectors.
CREATE TABLE IF NOT EXISTS dd_agent.assembly_rebuild_plans (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL,
    project_id uuid NOT NULL,
    document_id uuid NOT NULL,
    run_epoch bigint NOT NULL CHECK(run_epoch>0),
    policy_version text NOT NULL,
    vision_policy text NOT NULL,
    base_generation bigint NOT NULL CHECK(base_generation>0),
    reset_generation bigint NOT NULL CHECK(reset_generation>=0),
    reading_version text NOT NULL,
    page_count integer NOT NULL CHECK(page_count>0),
    scan_cursor bigint NOT NULL DEFAULT 0 CHECK(scan_cursor>=0),
    prepared_cursor bigint NOT NULL DEFAULT 0 CHECK(prepared_cursor>=0),
    status text NOT NULL DEFAULT 'scanning' CHECK(status IN
        ('scanning','ready','preparing','prepared','clean','blocked','superseded')),
    error_code text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(id,document_id,policy_version),
    FOREIGN KEY(job_id,project_id,document_id) REFERENCES dd_agent.processing_jobs(id,project_id,document_id)
);
CREATE INDEX IF NOT EXISTS assembly_rebuild_document ON dd_agent.assembly_rebuild_plans(document_id,created_at DESC);
CREATE TABLE IF NOT EXISTS dd_agent.assembly_rebuild_members (
    plan_id uuid NOT NULL,
    document_id uuid NOT NULL,
    policy_version text NOT NULL,
    receipt_generation bigint NOT NULL,
    input_dependencies jsonb NOT NULL CHECK(jsonb_typeof(input_dependencies)='array'),
    meta_hash text NOT NULL CHECK(length(meta_hash)=64),
    state_checksum text NOT NULL CHECK(length(state_checksum)=64),
    next_page integer NOT NULL CHECK(next_page>0),
    closed boolean NOT NULL,
    dirty boolean NOT NULL,
    PRIMARY KEY(plan_id,receipt_generation),
    FOREIGN KEY(plan_id,document_id,policy_version) REFERENCES dd_agent.assembly_rebuild_plans(id,document_id,policy_version),
    FOREIGN KEY(document_id,policy_version,receipt_generation) REFERENCES dd_agent.assembly_batches(document_id,policy_version,generation)
);
CREATE INDEX IF NOT EXISTS assembly_rebuild_dirty ON dd_agent.assembly_rebuild_members(plan_id,receipt_generation) WHERE dirty;
CREATE TABLE IF NOT EXISTS dd_agent.assembly_rebuild_sources (
    plan_id uuid NOT NULL REFERENCES dd_agent.assembly_rebuild_plans(id),
    page_number integer NOT NULL CHECK(page_number>0),
    revision text NOT NULL,
    status text NOT NULL CHECK(status IN ('succeeded','failed','missing')),
    quality text NOT NULL,
    PRIMARY KEY(plan_id,page_number)
);
CREATE TABLE IF NOT EXISTS dd_agent.assembly_rebuild_parts (
    plan_id uuid NOT NULL,
    receipt_generation bigint NOT NULL,
    region integer NOT NULL CHECK(region>0),
    before_checksum text,
    converged boolean NOT NULL,
    stop_reason text NOT NULL CHECK(stop_reason IN ('stable','document_end','propagate')),
    result jsonb NOT NULL CHECK(jsonb_typeof(result)='object'),
    seal text NOT NULL CHECK(length(seal)=64),
    stage_job_id uuid NOT NULL REFERENCES dd_agent.stage_jobs(id),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(plan_id,receipt_generation),
    FOREIGN KEY(plan_id,receipt_generation) REFERENCES dd_agent.assembly_rebuild_members(plan_id,receipt_generation)
);
INSERT INTO dd_agent.schema_version(version) VALUES(13) ON CONFLICT DO NOTHING;
