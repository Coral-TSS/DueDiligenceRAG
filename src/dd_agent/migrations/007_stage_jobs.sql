-- Durable handoff only. The stage consumer/window scheduler is introduced in RG-05d.
ALTER TABLE dd_agent.processing_jobs ADD COLUMN IF NOT EXISTS run_epoch bigint NOT NULL DEFAULT 1 CHECK(run_epoch>0);
ALTER TABLE dd_agent.processing_jobs ADD COLUMN IF NOT EXISTS stage_cursor bigint NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS processing_jobs_identity ON dd_agent.processing_jobs(id,project_id,document_id);

CREATE TABLE IF NOT EXISTS dd_agent.page_outbox (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES dd_agent.documents(id),
    page_number integer NOT NULL CHECK(page_number>0),
    stage text NOT NULL CHECK(stage IN ('vision','embedding')),
    source_revision text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(document_id,page_number,stage,source_revision)
);
CREATE INDEX IF NOT EXISTS page_outbox_document_cursor ON dd_agent.page_outbox(document_id,id);

CREATE TABLE IF NOT EXISTS dd_agent.stage_jobs (
    id uuid PRIMARY KEY,
    job_id uuid NOT NULL,
    project_id uuid NOT NULL,
    document_id uuid NOT NULL,
    stage text NOT NULL CHECK(stage IN ('preflight','reading','vision','assembly','embedding')),
    scope jsonb NOT NULL,
    scope_key text NOT NULL,
    input_revision text NOT NULL,
    dependencies jsonb NOT NULL DEFAULT '[]',
    depends_on uuid[] NOT NULL DEFAULT '{}',
    run_epoch bigint NOT NULL,
    owner_token uuid,
    state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','running','succeeded','failed','cancelled')),
    attempts integer NOT NULL DEFAULT 0,
    max_attempts integer NOT NULL DEFAULT 3 CHECK(max_attempts BETWEEN 1 AND 5),
    available_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    result_ref text NOT NULL DEFAULT '' CHECK(length(result_ref)<=512),
    error_code text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(job_id,project_id,document_id) REFERENCES dd_agent.processing_jobs(id,project_id,document_id),
    UNIQUE(job_id,stage,scope_key,input_revision,run_epoch)
);
CREATE INDEX IF NOT EXISTS stage_jobs_ready ON dd_agent.stage_jobs(job_id,run_epoch,stage,available_at,id) WHERE state='pending';
CREATE INDEX IF NOT EXISTS stage_jobs_reuse ON dd_agent.stage_jobs(job_id,stage,scope_key,input_revision) WHERE state='succeeded';
-- Sequence allocation is NOT commit order. Use a per-parent/epoch delivery ledger,
-- not id > last_seen, or a lower-id transaction that commits late would be lost.
CREATE TABLE IF NOT EXISTS dd_agent.stage_deliveries (
    job_id uuid NOT NULL REFERENCES dd_agent.processing_jobs(id),
    run_epoch bigint NOT NULL,
    event_id bigint NOT NULL REFERENCES dd_agent.page_outbox(id),
    PRIMARY KEY(job_id,run_epoch,event_id)
);
CREATE TABLE IF NOT EXISTS dd_agent.stage_attempts (
    stage_job_id uuid NOT NULL REFERENCES dd_agent.stage_jobs(id),
    attempt integer NOT NULL,
    owner_token uuid NOT NULL,
    started_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ended_at timestamptz,
    outcome text NOT NULL DEFAULT 'running',
    error_code text NOT NULL DEFAULT '',
    PRIMARY KEY(stage_job_id,attempt)
);

CREATE OR REPLACE FUNCTION dd_agent.record_page_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME='document_pages' THEN
        IF NEW.status='succeeded' THEN
            INSERT INTO dd_agent.page_outbox(document_id,page_number,stage,source_revision)
            VALUES(NEW.document_id,NEW.page_number,'vision',md5(NEW.pipeline_version||':'||NEW.updated_at::text))
            ON CONFLICT DO NOTHING;
        END IF;
    ELSE
        -- Only the actually effective, base-matching machine result creates downstream work.
        INSERT INTO dd_agent.page_outbox(document_id,page_number,stage,source_revision)
        SELECT e.document_id,e.page_number,'embedding',e.source_revision
        FROM dd_agent.effective_document_pages e
        JOIN dd_agent.document_pages p ON p.document_id=e.document_id AND p.page_number=e.page_number AND p.pipeline_version=e.pipeline_version
        WHERE e.document_id=NEW.document_id AND e.page_number=NEW.page_number
          AND e.raw_revision=NEW.base_revision
          AND e.source_revision=md5(p.pipeline_version||':'||p.updated_at::text||':'||NEW.id::text)
        ON CONFLICT DO NOTHING;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS page_outbox_saved ON dd_agent.document_pages;
CREATE TRIGGER page_outbox_saved AFTER INSERT OR UPDATE ON dd_agent.document_pages
    FOR EACH ROW EXECUTE FUNCTION dd_agent.record_page_outbox();
DROP TRIGGER IF EXISTS machine_outbox_saved ON dd_agent.machine_pages;
CREATE TRIGGER machine_outbox_saved AFTER INSERT ON dd_agent.machine_pages
    FOR EACH ROW EXECUTE FUNCTION dd_agent.record_page_outbox();

-- Re-runnable backfill: old successful pages are not lost just because they predate this migration.
INSERT INTO dd_agent.page_outbox(document_id,page_number,stage,source_revision)
SELECT document_id,page_number,'vision',md5(pipeline_version||':'||updated_at::text)
FROM dd_agent.document_pages WHERE status='succeeded' ON CONFLICT DO NOTHING;
INSERT INTO dd_agent.page_outbox(document_id,page_number,stage,source_revision)
SELECT e.document_id,e.page_number,'embedding',e.source_revision FROM dd_agent.effective_document_pages e
JOIN LATERAL (SELECT base_revision FROM dd_agent.machine_pages m
    WHERE m.document_id=e.document_id AND m.page_number=e.page_number ORDER BY created_at DESC,id DESC LIMIT 1) m
    ON m.base_revision=e.raw_revision WHERE e.status='succeeded' ON CONFLICT DO NOTHING;
INSERT INTO dd_agent.schema_version(version) VALUES(7) ON CONFLICT DO NOTHING;
