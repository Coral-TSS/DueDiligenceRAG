-- A receipt's generation is immutable identity; position is physical document order.
ALTER TABLE dd_agent.assembly_rebuild_members ADD COLUMN IF NOT EXISTS position bigint;
UPDATE dd_agent.assembly_rebuild_members SET position=receipt_generation WHERE position IS NULL;
ALTER TABLE dd_agent.assembly_rebuild_members ALTER COLUMN position SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS assembly_rebuild_position ON dd_agent.assembly_rebuild_members(plan_id,position);
ALTER TABLE dd_agent.assembly_rebuild_plans DROP CONSTRAINT IF EXISTS assembly_rebuild_plans_status_check;
ALTER TABLE dd_agent.assembly_rebuild_plans ADD CONSTRAINT assembly_rebuild_plans_status_check
    CHECK(status IN ('scanning','ready','preparing','prepared','clean','blocked','superseded','adopted'));

CREATE TABLE IF NOT EXISTS dd_agent.assembly_adoptions (
    plan_id uuid PRIMARY KEY REFERENCES dd_agent.assembly_rebuild_plans(id),
    status text NOT NULL DEFAULT 'validating' CHECK(status IN ('validating','ready','adopted','blocked','superseded')),
    cursor bigint NOT NULL DEFAULT 0,
    final_state jsonb,
    final_hash text,
    generation bigint,
    error_code text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS dd_agent.assembly_adoption_checks (
    plan_id uuid NOT NULL,
    receipt_generation bigint NOT NULL,
    seal text NOT NULL CHECK(length(seal)=64),
    payload_hash text NOT NULL CHECK(length(payload_hash)=64),
    PRIMARY KEY(plan_id,receipt_generation),
    FOREIGN KEY(plan_id,receipt_generation) REFERENCES dd_agent.assembly_rebuild_parts(plan_id,receipt_generation)
);
CREATE TABLE IF NOT EXISTS dd_agent.assembly_lineage_heads (
    document_id uuid NOT NULL,
    assembly_policy text NOT NULL,
    plan_id uuid NOT NULL REFERENCES dd_agent.assembly_adoptions(plan_id),
    reset_generation bigint NOT NULL,
    PRIMARY KEY(document_id,assembly_policy)
);
CREATE TABLE IF NOT EXISTS dd_agent.assembly_lineage_members (
    document_id uuid NOT NULL,
    assembly_policy text NOT NULL,
    position bigint NOT NULL CHECK(position>0),
    receipt_generation bigint NOT NULL,
    PRIMARY KEY(document_id,assembly_policy,position),
    UNIQUE(document_id,assembly_policy,receipt_generation),
    FOREIGN KEY(document_id,assembly_policy) REFERENCES dd_agent.assembly_lineage_heads(document_id,assembly_policy),
    FOREIGN KEY(document_id,assembly_policy,receipt_generation) REFERENCES dd_agent.assembly_receipt_outbox(document_id,assembly_policy,receipt_generation)
);
CREATE OR REPLACE FUNCTION dd_agent.assembly_receipt_current(doc uuid,policy text,gen bigint) RETURNS boolean
LANGUAGE sql STABLE AS $$
    WITH reset AS (SELECT coalesce(max(receipt_generation),0) AS n FROM dd_agent.assembly_receipt_outbox
        WHERE document_id=doc AND assembly_policy=policy AND is_reset),
    managed AS (SELECT 1 FROM dd_agent.assembly_lineage_heads h,reset r
        WHERE h.document_id=doc AND h.assembly_policy=policy AND h.reset_generation=r.n)
    SELECT CASE WHEN EXISTS(SELECT 1 FROM managed) THEN EXISTS(
        SELECT 1 FROM dd_agent.assembly_lineage_members m WHERE m.document_id=doc
        AND m.assembly_policy=policy AND m.receipt_generation=gen)
        ELSE gen>=(SELECT n FROM reset) END
$$;
CREATE OR REPLACE VIEW dd_agent.assembly_live_receipts AS
    SELECT o.*,coalesce(m.position,o.receipt_generation) AS position
    FROM dd_agent.assembly_receipt_outbox o LEFT JOIN dd_agent.assembly_lineage_members m
        ON m.document_id=o.document_id AND m.assembly_policy=o.assembly_policy AND m.receipt_generation=o.receipt_generation
    WHERE dd_agent.assembly_receipt_current(o.document_id,o.assembly_policy,o.receipt_generation);

CREATE OR REPLACE FUNCTION dd_agent.parent_build_origin_current(identity text) RETURNS boolean
LANGUAGE sql STABLE AS $$
    SELECT NOT EXISTS(SELECT 1 FROM dd_agent.assembly_handoff_parts WHERE build_id=identity)
      OR EXISTS(SELECT 1 FROM dd_agent.assembly_handoff_parts p WHERE p.build_id=identity
        AND dd_agent.assembly_receipt_current(p.document_id,p.assembly_policy,p.receipt_generation))
$$;
INSERT INTO dd_agent.schema_version(version) VALUES(14) ON CONFLICT DO NOTHING;
