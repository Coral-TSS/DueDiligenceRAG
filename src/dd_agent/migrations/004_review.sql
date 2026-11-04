CREATE TABLE IF NOT EXISTS dd_agent.page_adoptions (
    id uuid PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES dd_agent.documents(id),
    page_number integer NOT NULL CHECK (page_number > 0),
    base_revision text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('text_checked','unreadable')),
    text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS page_adoptions_history ON dd_agent.page_adoptions(document_id,page_number,created_at DESC);
CREATE TABLE IF NOT EXISTS dd_agent.vision_candidates (
    id uuid PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES dd_agent.documents(id),
    page_number integer NOT NULL,
    base_revision text NOT NULL,
    bounds jsonb NOT NULL,
    status text NOT NULL CHECK (status IN ('running','succeeded','failed','stale')),
    result jsonb NOT NULL DEFAULT '{}',
    error_code text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    finished_at timestamptz
);
ALTER TABLE dd_agent.indexed_pages ADD COLUMN IF NOT EXISTS source_revision text NOT NULL DEFAULT '';
CREATE OR REPLACE VIEW dd_agent.effective_document_pages AS
 SELECT p.document_id,p.page_number,p.pipeline_version,
   CASE WHEN p.status<>'succeeded' THEN p.status
        WHEN a.id IS NOT NULL AND a.base_revision<>md5(p.pipeline_version||':'||p.updated_at::text) THEN 'needs_review'
        WHEN a.decision='unreadable' THEN 'unreadable' ELSE p.status END AS status,
   p.method,coalesce(a.text,p.text) AS text,p.lines,p.image_key,p.error_code,
   greatest(p.updated_at,a.created_at) AS updated_at,
   md5(p.pipeline_version||':'||p.updated_at::text) AS raw_revision,
   md5(p.pipeline_version||':'||p.updated_at::text||':'||coalesce(a.id::text,'')) AS source_revision,
   CASE WHEN a.decision='text_checked' THEN 'text_checked' ELSE 'unverified' END AS verification
 FROM dd_agent.document_pages p
 JOIN dd_agent.document_readings r ON r.document_id=p.document_id AND r.pipeline_version=p.pipeline_version
 LEFT JOIN LATERAL (SELECT * FROM dd_agent.page_adoptions a
   WHERE a.document_id=p.document_id AND a.page_number=p.page_number ORDER BY a.created_at DESC,a.id DESC LIMIT 1) a ON true;
INSERT INTO dd_agent.schema_version (version) VALUES (4) ON CONFLICT DO NOTHING;
