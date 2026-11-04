"""自动窗口和局部重算共用的有界视觉就绪判断；不调用识别或模型。"""
from .assembly import digest


def vision_revision(number,raw,policy):
    scope=dict(pages=[number]);deps=[dict(page_number=number,view='raw',revision=raw)]
    return digest(dict(stage='vision',scope_key=digest(scope),source=raw,policy=policy,dependencies=deps,predecessors=[]))


def resolved_prefix(database,parent,document,start,limit,vision_policy,*,reading_done):
    numbers=list(range(start,min(start+limit,document['page_count']+1)))
    if not numbers:return [],set(),''
    # 这里只读修订、长度和机器策略，不加载待视觉页的基础正文。
    with database.connect() as conn:
        rows={r['page_number']:r for r in conn.execute('''SELECT e.page_number,e.status,e.raw_revision,
            length(e.text) AS characters,m.base_revision,m.profile_version
            FROM dd_agent.effective_document_pages e
            LEFT JOIN LATERAL (SELECT base_revision,profile_version FROM dd_agent.machine_pages m
                WHERE m.document_id=e.document_id AND m.page_number=e.page_number
                ORDER BY m.created_at DESC,m.id DESC LIMIT 1) m ON true
            WHERE e.document_id=%s AND e.page_number=ANY(%s)''',(parent['document_id'],numbers)).fetchall()}
        revisions=[vision_revision(n,r['raw_revision'],vision_policy) for n,r in rows.items() if r['status']=='succeeded']
        states={r['input_revision']:r['state'] for r in conn.execute('''SELECT input_revision,state FROM dd_agent.stage_jobs
            WHERE job_id=%s AND run_epoch=%s AND stage='vision' AND input_revision=ANY(%s)''',
            (parent['id'],parent['run_epoch'],revisions)).fetchall()}
    selected=[];ready=set();characters=0;reason=''
    for n in numbers:
        row=rows.get(n)
        if row is None:
            if not reading_done:reason='reading';break
        elif row['status']=='succeeded':
            is_ready=row['base_revision']==row['raw_revision'] and row['profile_version']==vision_policy
            state=states.get(vision_revision(n,row['raw_revision'],vision_policy))
            if not is_ready and state not in ('failed','cancelled'):
                reason='vision_artifact_missing' if state=='succeeded' else 'vision';break
            if is_ready:
                size=row['characters'] or 0
                # PageSnapshot 单页上限 100000；先于加载正文与构造快照拒绝。
                if size>100000:reason='assembly_input_too_large';break
                if characters+size>200000:reason='character_budget';break
                characters+=size;ready.add(n)
        elif row['status']!='failed':reason='reading';break
        selected.append(n)
    return selected,ready,reason

