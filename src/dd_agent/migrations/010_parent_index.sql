-- Independent inactive index family. No rewrite of indexed_pages/evidence_chunks.
CREATE TABLE IF NOT EXISTS dd_agent.parent_index_builds (
    id text PRIMARY KEY CHECK(length(id)=64),
    document_id uuid NOT NULL REFERENCES dd_agent.documents(id),
    index_version text NOT NULL CHECK(length(index_version)=64),
    input_revision text NOT NULL CHECK(length(input_revision)=64),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(document_id,index_version,input_revision)
);
CREATE TABLE IF NOT EXISTS dd_agent.parent_index_parents (
    build_id text NOT NULL REFERENCES dd_agent.parent_index_builds(id),
    id text NOT NULL CHECK(length(id)=64),
    text text NOT NULL CHECK(length(text) BETWEEN 1 AND 4000),
    source_spans jsonb NOT NULL CHECK(jsonb_typeof(source_spans)='array'),
    separators jsonb NOT NULL CHECK(jsonb_typeof(separators)='array'),
    dependencies jsonb NOT NULL CHECK(jsonb_typeof(dependencies)='array'),
    boundary text NOT NULL,
    continuation_from text,
    PRIMARY KEY(build_id,id)
);
CREATE TABLE IF NOT EXISTS dd_agent.parent_index_children (
    build_id text NOT NULL,
    parent_id text NOT NULL,
    id text NOT NULL CHECK(length(id)=64),
    ordinal integer NOT NULL CHECK(ordinal>=0),
    start_offset integer NOT NULL CHECK(start_offset>=0),
    end_offset integer NOT NULL CHECK(end_offset>start_offset),
    text text NOT NULL CHECK(length(text) BETWEEN 1 AND 700),
    source_spans jsonb NOT NULL CHECK(jsonb_typeof(source_spans)='array'),
    separators jsonb NOT NULL CHECK(jsonb_typeof(separators)='array'),
    embedding_tokens integer NOT NULL CHECK(embedding_tokens BETWEEN 1 AND 4096),
    reranker_tokens integer NOT NULL CHECK(reranker_tokens BETWEEN 1 AND 2048),
    embedding public.vector(1024) NOT NULL,
    PRIMARY KEY(build_id,id),
    UNIQUE(build_id,parent_id,ordinal),
    FOREIGN KEY(build_id,parent_id) REFERENCES dd_agent.parent_index_parents(build_id,id),
    CHECK(end_offset-start_offset=length(text))
);
CREATE TABLE IF NOT EXISTS dd_agent.parent_index_dependencies (
    build_id text NOT NULL REFERENCES dd_agent.parent_index_builds(id),
    document_id uuid NOT NULL REFERENCES dd_agent.documents(id),
    page_number integer NOT NULL CHECK(page_number>0),
    source_revision text NOT NULL,
    readiness text NOT NULL CHECK(readiness IN ('ready','missing','pending','failed')),
    quality text NOT NULL,
    PRIMARY KEY(build_id,page_number)
);
CREATE INDEX IF NOT EXISTS parent_index_reverse_dependency
    ON dd_agent.parent_index_dependencies(document_id,page_number,build_id);
INSERT INTO dd_agent.schema_version(version) VALUES(10) ON CONFLICT DO NOTHING;
