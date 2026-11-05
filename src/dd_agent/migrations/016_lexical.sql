-- 页片段和父子片段的全文词项。NULL 表示旧数据尚未补建，空 tsvector 表示文本没有可索引词项。
ALTER TABLE dd_agent.evidence_chunks ADD COLUMN IF NOT EXISTS lexemes tsvector;
CREATE INDEX IF NOT EXISTS evidence_chunks_lexemes ON dd_agent.evidence_chunks USING GIN (lexemes);
ALTER TABLE dd_agent.parent_index_children ADD COLUMN IF NOT EXISTS lexemes tsvector;
CREATE INDEX IF NOT EXISTS parent_index_children_lexemes ON dd_agent.parent_index_children USING GIN (lexemes);
INSERT INTO dd_agent.schema_version(version) VALUES (16) ON CONFLICT DO NOTHING;
