-- Receipt + tail checkpoint + outbox share the AssemblyStore transaction.
CREATE TABLE IF NOT EXISTS dd_agent.assembly_receipt_outbox (
    document_id uuid NOT NULL,
    assembly_policy text NOT NULL,
    receipt_generation bigint NOT NULL,
    is_reset boolean NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(document_id,assembly_policy,receipt_generation),
    FOREIGN KEY(document_id,assembly_policy,receipt_generation)
      REFERENCES dd_agent.assembly_batches(document_id,policy_version,generation)
);
CREATE INDEX IF NOT EXISTS assembly_latest_reset
    ON dd_agent.assembly_receipt_outbox(document_id,assembly_policy,receipt_generation DESC) WHERE is_reset;
CREATE OR REPLACE FUNCTION dd_agent.record_assembly_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO dd_agent.assembly_receipt_outbox(document_id,assembly_policy,receipt_generation,is_reset)
      VALUES(NEW.document_id,NEW.policy_version,NEW.generation,NEW.result->'state'='null'::jsonb)
      ON CONFLICT DO NOTHING;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS assembly_receipt_saved ON dd_agent.assembly_batches;
CREATE TRIGGER assembly_receipt_saved AFTER INSERT ON dd_agent.assembly_batches
    FOR EACH ROW EXECUTE FUNCTION dd_agent.record_assembly_receipt();
INSERT INTO dd_agent.assembly_receipt_outbox(document_id,assembly_policy,receipt_generation,is_reset)
    SELECT document_id,policy_version,generation,result->'state'='null'::jsonb
    FROM dd_agent.assembly_batches ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS dd_agent.assembly_handoffs (
    job_id uuid NOT NULL,
    run_epoch bigint NOT NULL CHECK(run_epoch>0),
    index_version text NOT NULL CHECK(length(index_version)=64),
    project_id uuid NOT NULL,
    document_id uuid NOT NULL,
    assembly_policy text NOT NULL,
    receipt_generation bigint NOT NULL,
    cursor integer NOT NULL DEFAULT 0 CHECK(cursor BETWEEN 0 AND 2000),
    state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','delivered','empty','stale')),
    error_code text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(job_id,run_epoch,index_version,document_id,assembly_policy,receipt_generation),
    FOREIGN KEY(job_id,project_id,document_id) REFERENCES dd_agent.processing_jobs(id,project_id,document_id),
    FOREIGN KEY(document_id,assembly_policy,receipt_generation)
      REFERENCES dd_agent.assembly_receipt_outbox(document_id,assembly_policy,receipt_generation)
);
CREATE TABLE IF NOT EXISTS dd_agent.assembly_handoff_parts (
    job_id uuid NOT NULL,
    run_epoch bigint NOT NULL,
    index_version text NOT NULL,
    document_id uuid NOT NULL,
    assembly_policy text NOT NULL,
    receipt_generation bigint NOT NULL,
    start_parent integer NOT NULL CHECK(start_parent>=0),
    end_parent integer NOT NULL CHECK(end_parent>start_parent AND end_parent<=2000),
    build_id text NOT NULL REFERENCES dd_agent.parent_index_work(id),
    stage_job_id uuid NOT NULL REFERENCES dd_agent.stage_jobs(id),
    PRIMARY KEY(job_id,run_epoch,index_version,document_id,assembly_policy,receipt_generation,start_parent),
    FOREIGN KEY(job_id,run_epoch,index_version,document_id,assembly_policy,receipt_generation)
      REFERENCES dd_agent.assembly_handoffs(job_id,run_epoch,index_version,document_id,assembly_policy,receipt_generation)
);
CREATE INDEX IF NOT EXISTS assembly_handoff_reuse ON dd_agent.assembly_handoff_parts
    (job_id,index_version,document_id,assembly_policy,receipt_generation,start_parent,run_epoch DESC);
CREATE INDEX IF NOT EXISTS assembly_build_origin ON dd_agent.assembly_handoff_parts(build_id);

-- Identical work may be adopted by a new current receipt. Manual b1 work has no origin yet.
CREATE OR REPLACE FUNCTION dd_agent.parent_build_origin_current(identity text) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT NOT EXISTS(SELECT 1 FROM dd_agent.assembly_handoff_parts WHERE build_id=identity)
      OR EXISTS(SELECT 1 FROM dd_agent.assembly_handoff_parts p WHERE p.build_id=identity
        AND p.receipt_generation>=coalesce((SELECT max(o.receipt_generation)
          FROM dd_agent.assembly_receipt_outbox o WHERE o.document_id=p.document_id
          AND o.assembly_policy=p.assembly_policy AND o.is_reset),0))
$$;
INSERT INTO dd_agent.schema_version(version) VALUES(12) ON CONFLICT DO NOTHING;
