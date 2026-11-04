CREATE TABLE IF NOT EXISTS dd_agent.machine_pages (
    id uuid PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES dd_agent.documents(id),
    page_number integer NOT NULL CHECK (page_number>0),
    base_revision text NOT NULL,
    profile_version text NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS machine_pages_latest ON dd_agent.machine_pages(document_id,page_number,created_at DESC);
CREATE OR REPLACE VIEW dd_agent.effective_document_pages AS
 SELECT p.document_id,p.page_number,p.pipeline_version,p.status,p.method,
   CASE WHEN m.base_revision=md5(p.pipeline_version||':'||p.updated_at::text) THEN m.result->>'text' ELSE p.text END AS text,
   p.lines,p.image_key,p.error_code,
   CASE WHEN m.base_revision=md5(p.pipeline_version||':'||p.updated_at::text) THEN greatest(p.updated_at,m.created_at) ELSE p.updated_at END AS updated_at,
   md5(p.pipeline_version||':'||p.updated_at::text) AS raw_revision,
   md5(p.pipeline_version||':'||p.updated_at::text||':'||
     CASE WHEN m.base_revision=md5(p.pipeline_version||':'||p.updated_at::text) THEN m.id::text ELSE '' END) AS source_revision,
   CASE WHEN m.base_revision=md5(p.pipeline_version||':'||p.updated_at::text) THEN m.result->>'verification' ELSE 'unverified' END AS verification
 FROM dd_agent.document_pages p
 JOIN dd_agent.document_readings r ON r.document_id=p.document_id AND r.pipeline_version=p.pipeline_version
 LEFT JOIN LATERAL (SELECT * FROM dd_agent.machine_pages m WHERE m.document_id=p.document_id AND m.page_number=p.page_number
     ORDER BY m.created_at DESC,m.id DESC LIMIT 1) m ON true;
CREATE TABLE IF NOT EXISTS dd_agent.processing_jobs (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES dd_agent.projects(id),
    document_id uuid NOT NULL REFERENCES dd_agent.documents(id),
    status text NOT NULL CHECK (status IN ('queued','running','completed','partial','failed')),
    progress jsonb NOT NULL DEFAULT '{}',
    error_code text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX IF NOT EXISTS processing_job_active ON dd_agent.processing_jobs(document_id) WHERE status IN ('queued','running');
INSERT INTO dd_agent.schema_version(version) VALUES (5) ON CONFLICT DO NOTHING;
