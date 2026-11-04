CREATE SCHEMA IF NOT EXISTS dd_agent;
CREATE TABLE IF NOT EXISTS dd_agent.schema_version (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dd_agent.projects (
    id uuid PRIMARY KEY,
    name text NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS dd_agent.documents (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES dd_agent.projects(id),
    sha256 char(64) NOT NULL,
    byte_size bigint NOT NULL CHECK (byte_size >= 0),
    storage_key text NOT NULL,
    status text NOT NULL CHECK (status IN ('registered', 'unsupported', 'invalid')),
    reason text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (project_id, sha256)
);
CREATE TABLE IF NOT EXISTS dd_agent.document_sources (
    document_id uuid NOT NULL REFERENCES dd_agent.documents(id),
    filename text NOT NULL,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id, filename)
);
INSERT INTO dd_agent.schema_version (version) VALUES (1) ON CONFLICT DO NOTHING;
