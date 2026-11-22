-- 材料类别改为固定尽调目录。已有文书种类代码按对照表改写；访谈记录并入其他。
-- CHECK 不能原地替换取值，所以先删约束、再改写、再按新目录列表重建。
-- 本文件可重复执行：新目录代码在对照表中原样保留，不会在重跑时被改成未分类。
DO $$ BEGIN
    ALTER TABLE dd_agent.documents DROP CONSTRAINT IF EXISTS documents_category_known;

    UPDATE dd_agent.documents SET category = CASE category
        WHEN 'uncategorized' THEN 'uncategorized'
        WHEN 'business_license' THEN 'incorporation'
        WHEN 'articles' THEN 'organizational_docs'
        WHEN 'shareholder_resolution' THEN 'organizational_docs'
        WHEN 'board_resolution' THEN 'organizational_docs'
        WHEN 'equity_structure' THEN 'shareholders'
        WHEN 'audit_report' THEN 'finance'
        WHEN 'financial_statement' THEN 'finance'
        WHEN 'major_contract' THEN 'major_contracts'
        WHEN 'litigation' THEN 'litigation_admin'
        WHEN 'ip_certificate' THEN 'ip'
        WHEN 'regulatory_filing' THEN 'other'
        WHEN 'hr_labor' THEN 'labor_hr'
        WHEN 'interview_record' THEN 'other'
        WHEN 'other' THEN 'other'
        WHEN 'incorporation' THEN 'incorporation'
        WHEN 'organizational_docs' THEN 'organizational_docs'
        WHEN 'shareholders' THEN 'shareholders'
        WHEN 'outbound_investment' THEN 'outbound_investment'
        WHEN 'business' THEN 'business'
        WHEN 'major_contracts' THEN 'major_contracts'
        WHEN 'major_assets' THEN 'major_assets'
        WHEN 'ip' THEN 'ip'
        WHEN 'litigation_admin' THEN 'litigation_admin'
        WHEN 'environmental' THEN 'environmental'
        WHEN 'product_quality' THEN 'product_quality'
        WHEN 'labor_hr' THEN 'labor_hr'
        WHEN 'finance' THEN 'finance'
        WHEN 'attachments' THEN 'attachments'
        WHEN 'subsidiary_aic' THEN 'subsidiary_aic'
        ELSE 'uncategorized'
    END;

    ALTER TABLE dd_agent.documents ADD CONSTRAINT documents_category_known CHECK (category IN (
        'uncategorized', 'incorporation', 'organizational_docs', 'shareholders',
        'outbound_investment', 'business', 'major_contracts', 'major_assets',
        'ip', 'litigation_admin', 'environmental', 'product_quality',
        'labor_hr', 'finance', 'attachments', 'subsidiary_aic', 'other'));
END $$;

INSERT INTO dd_agent.schema_version(version) VALUES (21) ON CONFLICT DO NOTHING;
