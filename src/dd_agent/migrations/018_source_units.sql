-- 现代 Office 的来源单元。不占用 PDF 页码，也不进入页级父子组装。
CREATE TABLE IF NOT EXISTS dd_agent.source_units (
    id uuid PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES dd_agent.documents(id),
    locator jsonb NOT NULL,
    locator_key text NOT NULL,
    text text NOT NULL DEFAULT '',
    formula_state text NOT NULL DEFAULT '' CHECK (formula_state IN ('', 'not_recalculated', 'unknown')),
    parse_version text NOT NULL,
    status text NOT NULL CHECK (status IN ('succeeded', 'failed')),
    error_code text NOT NULL DEFAULT '',
    UNIQUE (document_id, locator_key, parse_version)
);
CREATE TABLE IF NOT EXISTS dd_agent.source_unit_chunks (
    id uuid PRIMARY KEY,
    unit_id uuid NOT NULL REFERENCES dd_agent.source_units(id) ON DELETE CASCADE,
    document_id uuid NOT NULL,
    index_version text NOT NULL,
    ordinal integer NOT NULL CHECK (ordinal >= 0),
    text text NOT NULL,
    embedding public.vector(1024) NOT NULL,
    lexemes tsvector,
    locator jsonb NOT NULL,
    UNIQUE (unit_id, index_version, ordinal)
);
CREATE INDEX IF NOT EXISTS source_unit_chunks_lexemes ON dd_agent.source_unit_chunks USING GIN (lexemes);
CREATE INDEX IF NOT EXISTS source_unit_chunks_document ON dd_agent.source_unit_chunks(document_id, index_version);
INSERT INTO dd_agent.schema_version(version) VALUES (18) ON CONFLICT DO NOTHING;
