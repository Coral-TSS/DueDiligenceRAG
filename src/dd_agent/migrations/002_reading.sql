CREATE TABLE IF NOT EXISTS dd_agent.document_readings (
    document_id uuid PRIMARY KEY REFERENCES dd_agent.documents(id),
    page_count integer NOT NULL CHECK (page_count > 0),
    pipeline_version text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dd_agent.document_pages (
    document_id uuid NOT NULL REFERENCES dd_agent.documents(id),
    page_number integer NOT NULL CHECK (page_number > 0),
    pipeline_version text NOT NULL,
    status text NOT NULL CHECK (status IN ('succeeded', 'failed')),
    method text NOT NULL CHECK (method IN ('text', 'ocr')),
    text text NOT NULL DEFAULT '',
    lines jsonb NOT NULL DEFAULT '[]',
    image_key text NOT NULL DEFAULT '',
    error_code text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, page_number, pipeline_version)
);
INSERT INTO dd_agent.schema_version (version) VALUES (2) ON CONFLICT DO NOTHING;
