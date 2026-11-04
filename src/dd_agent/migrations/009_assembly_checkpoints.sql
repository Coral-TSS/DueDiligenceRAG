-- Standalone assembly receipts, not the active parent/child vector index.
CREATE TABLE IF NOT EXISTS dd_agent.assembly_checkpoints (
    document_id uuid NOT NULL REFERENCES dd_agent.documents(id),
    policy_version text NOT NULL CHECK(length(policy_version) BETWEEN 1 AND 256),
    generation bigint NOT NULL CHECK(generation>0),
    state jsonb CHECK(state IS NULL OR jsonb_typeof(state)='object'),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(document_id,policy_version)
);
CREATE TABLE IF NOT EXISTS dd_agent.assembly_batches (
    document_id uuid NOT NULL REFERENCES dd_agent.documents(id),
    policy_version text NOT NULL CHECK(length(policy_version) BETWEEN 1 AND 256),
    generation bigint NOT NULL CHECK(generation>0),
    request_key text NOT NULL CHECK(length(request_key)=64),
    result jsonb NOT NULL CHECK(jsonb_typeof(result)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(document_id,policy_version,generation),
    UNIQUE(document_id,policy_version,request_key)
);
INSERT INTO dd_agent.schema_version(version) VALUES(9) ON CONFLICT DO NOTHING;
