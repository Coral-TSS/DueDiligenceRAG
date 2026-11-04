-- A small per-job coordinator ledger, never another document or vector store.
CREATE TABLE IF NOT EXISTS dd_agent.assembly_repair_runs (
    job_id uuid PRIMARY KEY,
    project_id uuid NOT NULL,
    document_id uuid NOT NULL,
    run_epoch bigint NOT NULL CHECK(run_epoch>0),
    vision_policy text NOT NULL,
    base_generation bigint NOT NULL,
    reading_version text NOT NULL,
    page_count integer NOT NULL,
    source_stamp text NOT NULL,
    phase text NOT NULL DEFAULT 'probing' CHECK(phase IN
        ('probing','starting','scanning','preparing','validating','adopting','done','blocked')),
    cursor integer NOT NULL DEFAULT 1 CHECK(cursor>0),
    plan_id uuid REFERENCES dd_agent.assembly_rebuild_plans(id),
    owner_token uuid,
    attempts integer NOT NULL DEFAULT 0 CHECK(attempts BETWEEN 0 AND 3),
    failures integer NOT NULL DEFAULT 0 CHECK(failures BETWEEN 0 AND 3),
    adopted_generation bigint,
    error_code text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(job_id,project_id,document_id) REFERENCES dd_agent.processing_jobs(id,project_id,document_id)
);
INSERT INTO dd_agent.schema_version(version) VALUES(15) ON CONFLICT DO NOTHING;
