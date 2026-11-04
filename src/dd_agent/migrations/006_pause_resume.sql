ALTER TABLE dd_agent.processing_jobs DROP CONSTRAINT IF EXISTS processing_jobs_status_check;
ALTER TABLE dd_agent.processing_jobs ADD CONSTRAINT processing_jobs_status_check
 CHECK (status IN ('queued','running','pause_requested','paused','completed','partial','failed'));
DROP INDEX IF EXISTS dd_agent.processing_job_active;
CREATE UNIQUE INDEX processing_job_active ON dd_agent.processing_jobs(document_id)
 WHERE status IN ('queued','running','pause_requested','paused');
ALTER TABLE dd_agent.vision_candidates ADD COLUMN IF NOT EXISTS profile_version text NOT NULL DEFAULT '';
INSERT INTO dd_agent.schema_version(version) VALUES (6) ON CONFLICT DO NOTHING;
