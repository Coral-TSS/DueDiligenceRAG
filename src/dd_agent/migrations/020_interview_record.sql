-- 材料类别增加访谈记录。已有类别代码不改。CHECK 不能原地追加取值，所以删掉后按完整列表重建。
-- 若 021/022 已改用新的尽调目录代码，不要再重建本文件的旧约束，否则会与现有行冲突。
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM dd_agent.schema_version WHERE version >= 21) THEN
        NULL;
    ELSE
        ALTER TABLE dd_agent.documents DROP CONSTRAINT IF EXISTS documents_category_known;
        ALTER TABLE dd_agent.documents ADD CONSTRAINT documents_category_known CHECK (category IN (
            'uncategorized', 'business_license', 'articles', 'shareholder_resolution', 'board_resolution',
            'equity_structure', 'audit_report', 'financial_statement', 'major_contract', 'litigation',
            'ip_certificate', 'regulatory_filing', 'hr_labor', 'interview_record', 'other'));
    END IF;
END $$;

INSERT INTO dd_agent.schema_version(version) VALUES (20) ON CONFLICT DO NOTHING;
