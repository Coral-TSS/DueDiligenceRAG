-- 片段可以记下跨页来源。单页切片只有一条，跨页续段按页保留起止。
ALTER TABLE dd_agent.evidence_chunks
    ADD COLUMN IF NOT EXISTS source_spans jsonb NOT NULL DEFAULT '[]'::jsonb;
INSERT INTO dd_agent.schema_version(version) VALUES (17) ON CONFLICT DO NOTHING;
