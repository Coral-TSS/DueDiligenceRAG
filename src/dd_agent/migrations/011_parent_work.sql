-- Unpublished prepared work is independent of published build history.
CREATE TABLE IF NOT EXISTS dd_agent.parent_index_work (
    id text PRIMARY KEY CHECK(length(id)=64),
    document_id uuid NOT NULL REFERENCES dd_agent.documents(id),
    index_version text NOT NULL CHECK(length(index_version)=64),
    input_revision text NOT NULL CHECK(length(input_revision)=64),
    plan jsonb NOT NULL CHECK(jsonb_typeof(plan)='object'),
    plan_seal text NOT NULL CHECK(length(plan_seal)=64),
    vectors jsonb,
    vector_seal text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK(plan->>'id'=id AND plan->>'document_id'=document_id::text
      AND plan->>'index_version'=index_version AND plan->>'input_revision'=input_revision
      AND plan->>'seal'=plan_seal AND plan->'vectors'='null'::jsonb),
    CHECK(octet_length(plan::text)<=16777216),
    CHECK((vectors IS NULL AND vector_seal IS NULL) OR
      (vectors IS NOT NULL AND jsonb_typeof(vectors)='array'
       AND vector_seal IS NOT NULL AND length(vector_seal)=64 AND octet_length(vectors::text)<=8388608))
);
CREATE INDEX IF NOT EXISTS parent_work_document_version ON dd_agent.parent_index_work(document_id,index_version,id);
INSERT INTO dd_agent.schema_version(version) VALUES(11) ON CONFLICT DO NOTHING;
