ALTER TABLE dd_agent.processing_jobs ADD COLUMN IF NOT EXISTS paused_stages text[] NOT NULL DEFAULT '{}'
 CHECK(paused_stages <@ ARRAY['reading','vision','embedding']::text[]);
ALTER TABLE dd_agent.stage_jobs ADD COLUMN IF NOT EXISTS failures integer NOT NULL DEFAULT 0;
UPDATE dd_agent.stage_jobs s SET failures=greatest(s.failures,
 (SELECT count(*) FROM dd_agent.stage_attempts a WHERE a.stage_job_id=s.id AND a.outcome IN ('transient','permanent','stale')));
INSERT INTO dd_agent.schema_version(version) VALUES(8) ON CONFLICT DO NOTHING;
