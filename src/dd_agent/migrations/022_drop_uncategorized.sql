-- 去掉「未分类」。已有 uncategorized 与无法识别的残留一律并入「其他」。
-- CHECK 不能原地替换取值，所以先删约束、再改写、再按不含 uncategorized 的目录列表重建。
-- 本文件可重复执行：现行有效目录代码保持原样，已是 other 的保持 other。
DO $$ BEGIN
    ALTER TABLE dd_agent.documents DROP CONSTRAINT IF EXISTS documents_category_known;

    UPDATE dd_agent.documents SET category = CASE category
        WHEN 'uncategorized' THEN 'other'
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
        WHEN 'other' THEN 'other'
        ELSE 'other'
    END;

    ALTER TABLE dd_agent.documents ALTER COLUMN category SET DEFAULT 'other';

    ALTER TABLE dd_agent.documents ADD CONSTRAINT documents_category_known CHECK (category IN (
        'incorporation', 'organizational_docs', 'shareholders',
        'outbound_investment', 'business', 'major_contracts', 'major_assets',
        'ip', 'litigation_admin', 'environmental', 'product_quality',
        'labor_hr', 'finance', 'attachments', 'subsidiary_aic', 'other'));
END $$;

INSERT INTO dd_agent.schema_version(version) VALUES (22) ON CONFLICT DO NOTHING;
