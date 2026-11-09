-- 项目主体与材料的类别、主体、日期或期间。不记录可信度或管理类字段，改这些列不表示正文已变。
CREATE TABLE IF NOT EXISTS dd_agent.project_subjects (
    id uuid PRIMARY KEY,
    project_id uuid NOT NULL REFERENCES dd_agent.projects(id),
    name text NOT NULL,
    role text NOT NULL DEFAULT 'other',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT project_subjects_project_id_id UNIQUE (project_id, id),
    CONSTRAINT project_subjects_name_trimmed CHECK (name = btrim(name) AND char_length(name) BETWEEN 1 AND 200),
    CONSTRAINT project_subjects_role_known CHECK (role IN ('target', 'subsidiary', 'shareholder', 'counterparty', 'other'))
);
CREATE UNIQUE INDEX IF NOT EXISTS project_subjects_name_ci
    ON dd_agent.project_subjects (project_id, lower(name));
CREATE INDEX IF NOT EXISTS project_subjects_project ON dd_agent.project_subjects (project_id);

ALTER TABLE dd_agent.documents
    ADD COLUMN IF NOT EXISTS category text NOT NULL DEFAULT 'uncategorized',
    ADD COLUMN IF NOT EXISTS subject_id uuid,
    ADD COLUMN IF NOT EXISTS date_kind text NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS point_date date,
    ADD COLUMN IF NOT EXISTS period_start date,
    ADD COLUMN IF NOT EXISTS period_end date;

DO $$ BEGIN
    ALTER TABLE dd_agent.documents ADD CONSTRAINT documents_category_known CHECK (category IN (
        'uncategorized', 'business_license', 'articles', 'shareholder_resolution', 'board_resolution',
        'equity_structure', 'audit_report', 'financial_statement', 'major_contract', 'litigation',
        'ip_certificate', 'regulatory_filing', 'hr_labor', 'other'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE dd_agent.documents ADD CONSTRAINT documents_date_kind_known CHECK (date_kind IN ('none', 'point', 'period'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE dd_agent.documents ADD CONSTRAINT documents_date_shape CHECK (
        (date_kind = 'none' AND point_date IS NULL AND period_start IS NULL AND period_end IS NULL)
        OR (date_kind = 'point' AND point_date IS NOT NULL AND period_start IS NULL AND period_end IS NULL)
        OR (date_kind = 'period' AND point_date IS NULL AND period_start IS NOT NULL
            AND period_end IS NOT NULL AND period_start <= period_end));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    ALTER TABLE dd_agent.documents ADD CONSTRAINT documents_subject_same_project
        FOREIGN KEY (project_id, subject_id) REFERENCES dd_agent.project_subjects (project_id, id);
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

CREATE INDEX IF NOT EXISTS documents_project_category ON dd_agent.documents (project_id, category);
CREATE INDEX IF NOT EXISTS documents_project_subject ON dd_agent.documents (project_id, subject_id);

INSERT INTO dd_agent.schema_version(version) VALUES (19) ON CONFLICT DO NOTHING;
