CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE TABLE IF NOT EXISTS dd_agent.indexed_pages (
    document_id uuid NOT NULL REFERENCES dd_agent.documents(id),
    page_number integer NOT NULL,
    index_version text NOT NULL,
    pipeline_version text NOT NULL,
    source_updated_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, page_number, index_version)
);
CREATE TABLE IF NOT EXISTS dd_agent.evidence_chunks (
    id uuid PRIMARY KEY,
    document_id uuid NOT NULL,
    page_number integer NOT NULL,
    index_version text NOT NULL,
    ordinal integer NOT NULL,
    start_offset integer NOT NULL CHECK (start_offset >= 0),
    end_offset integer NOT NULL CHECK (end_offset > start_offset),
    text text NOT NULL,
    embedding public.vector(1024) NOT NULL,
    FOREIGN KEY (document_id, page_number, index_version)
        REFERENCES dd_agent.indexed_pages(document_id, page_number, index_version) ON DELETE CASCADE,
    UNIQUE (document_id, page_number, index_version, ordinal)
);
CREATE INDEX IF NOT EXISTS evidence_chunks_document_version
    ON dd_agent.evidence_chunks(document_id, index_version);
INSERT INTO dd_agent.schema_version (version) VALUES (3) ON CONFLICT DO NOTHING;
