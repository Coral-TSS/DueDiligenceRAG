"""Durable stage handoff, bounded claiming and fenced SQL publication (K04).

No model inference belongs here. A consumer calculates outside transactions, then
passes only a short SQL publication callback to complete(). RG-05d installs that
consumer; today's serial pipeline continues to produce reliable outbox events.
"""
from contextlib import nullcontext, contextmanager
import hashlib
import json
import re
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from .performance import timed

STAGES = {'preflight', 'reading', 'vision', 'assembly', 'embedding'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class StageJobs:
    def __init__(self, database):
        self.database = database

    def _scope(self, conn=None):
        return nullcontext(conn) if conn is not None else self.database.connect()

    @staticmethod
    def _parent(conn, job_id, exclusive=False):
        row = conn.execute('SELECT * FROM dd_agent.processing_jobs WHERE id=%s ' +
            ('FOR UPDATE' if exclusive else 'FOR SHARE'), (job_id,)).fetchone()
        if row is None:
            raise ValueError('父任务不存在。')
        return row

    @staticmethod
    def _sources(conn, document_id, dependencies, lock=False):
        if not dependencies:
            return True
        numbers = sorted({d['page_number'] for d in dependencies})
        reading=None
        if lock:
            # Fixed publication order: parent -> reading -> ascending pages -> stage row.
            reading = conn.execute('SELECT pipeline_version FROM dd_agent.document_readings WHERE document_id=%s FOR UPDATE',
                (document_id,)).fetchone()
            if not reading:
                return False
            conn.execute('SELECT page_number FROM dd_agent.document_pages WHERE document_id=%s AND pipeline_version=%s AND page_number=ANY(%s) ORDER BY page_number FOR UPDATE',
                (document_id, reading['pipeline_version'], numbers)).fetchall()
        if reading is None and any(d.get('readiness')=='missing' for d in dependencies):
            reading=conn.execute('SELECT pipeline_version FROM dd_agent.document_readings WHERE document_id=%s',
                (document_id,)).fetchone()
        pages = {r['page_number']: r for r in conn.execute('''SELECT page_number,status,raw_revision,source_revision,verification
            FROM dd_agent.effective_document_pages WHERE document_id=%s AND page_number=ANY(%s)''', (document_id, numbers)).fetchall()}
        for dep in dependencies:
            page=pages.get(dep['page_number']);readiness=dep.get('readiness','ready')
            # 只有 assembly 可登记显式边界；缺页由 reading 行锁保护并绑定读取版本。
            if readiness=='missing':
                if page is not None or not reading or dep['revision']!='missing:'+reading['pipeline_version']:return False
                continue
            if page is None or page['raw_revision' if dep['view']=='raw' else 'source_revision']!=dep['revision']:return False
            if readiness=='ready' and page['status']!='succeeded':return False
            if readiness=='failed' and page['status']!='failed':return False
            if 'readiness' in dep and page['verification']!=dep['quality']:return False
        return True

    @staticmethod
    def _end_attempt(conn, task_id, outcome, code=''):
        conn.execute('''UPDATE dd_agent.stage_attempts SET ended_at=clock_timestamp(),outcome=%s,error_code=%s
            WHERE stage_job_id=%s AND ended_at IS NULL''', (outcome, code, task_id))

    @timed('stage.ensure')
    def ensure(self, job_id, stage, scope, *, source_revision, policy_version,
               dependencies=None, depends_on=None, max_attempts=3, conn=None, reuse=True):
        if stage not in STAGES or not isinstance(scope, dict) or not source_revision or not policy_version:
            raise ValueError('阶段、范围、来源与策略版本必须明确。')
        if type(max_attempts) is not int or not 1 <= max_attempts <= 5:
            raise ValueError('重试次数须在 1–5 之间。')
        scope = dict(scope)
        if 'pages' in scope:
            pages = scope['pages']
            if not isinstance(pages, list) or not pages or len(pages)>100 or any(type(n) is not int or n<1 for n in pages):
                raise ValueError('阶段范围最多 100 个正整数页号。')
            scope['pages'] = sorted(set(pages))
        if len(canonical(scope))>8192:
            raise ValueError('阶段范围过大。')
        deps = [dict(d) for d in dependencies or []]
        if len(deps)>100 or any(d.get('view') not in ('raw','effective') or type(d.get('page_number')) is not int
            or d['page_number']<1 or not isinstance(d.get('revision'),str) or not d['revision'] for d in deps):
            raise ValueError('来源依赖格式无效。')
        for dep in deps:
            if 'readiness' in dep and (stage!='assembly' or dep['view']!='effective'
                or dep['readiness'] not in ('ready','failed','missing','pending')
                or not isinstance(dep.get('quality'),str) or not dep['quality']):
                raise ValueError('只有组装阶段可以绑定显式就绪／缺口来源。')
        deps.sort(key=lambda d: (d['page_number'],d['view'],d['revision']))
        if stage in ('vision','assembly','embedding'):
            expected_view = 'raw' if stage=='vision' else 'effective'
            bound_pages = {d['page_number'] for d in deps if d['view']==expected_view}
            if not scope.get('pages') or not set(scope['pages']).issubset(bound_pages):
                raise ValueError('页阶段必须绑定全部输入页的来源修订。')
        predecessors = sorted({UUID(str(i)) for i in depends_on or []})
        if len(predecessors)>100:
            raise ValueError('阶段前置依赖过多。')
        scope_key = digest(scope)
        with self._scope(conn) as conn:
            parent = self._parent(conn, job_id, exclusive=True)
            if parent['status'] in ('completed','partial','failed'):
                raise ValueError('父任务已结束。')
            predecessor_revisions = []
            if predecessors:
                inputs = conn.execute('SELECT input_revision FROM dd_agent.stage_jobs WHERE job_id=%s AND id=ANY(%s)',
                    (job_id, predecessors)).fetchall()
                if len(inputs) != len(predecessors):
                    raise ValueError('前置阶段不存在或不属于当前父任务。')
                predecessor_revisions = sorted(r['input_revision'] for r in inputs)
            revision = digest(dict(stage=stage,scope_key=scope_key,source=str(source_revision), policy=str(policy_version),
                dependencies=deps, predecessors=predecessor_revisions))
            reused = conn.execute('''SELECT * FROM dd_agent.stage_jobs WHERE job_id=%s AND stage=%s
                AND scope_key=%s AND input_revision=%s AND state='succeeded' ORDER BY run_epoch DESC LIMIT 1''',
                (job_id,stage,scope_key,revision)).fetchone()
            if reuse and reused and self._sources(conn,parent['document_id'],deps):
                return dict(reused,reused=True)
            if not reuse:
                conn.execute('''UPDATE dd_agent.stage_jobs SET state='pending',owner_token=NULL,result_ref='',updated_at=clock_timestamp()
                    WHERE job_id=%s AND stage=%s AND scope_key=%s AND input_revision=%s AND run_epoch=%s AND state='succeeded' ''',
                    (job_id,stage,scope_key,revision,parent['run_epoch']))
            cancelled = conn.execute('''UPDATE dd_agent.stage_jobs SET state='cancelled',error_code='input_changed',updated_at=clock_timestamp()
                WHERE job_id=%s AND stage=%s AND scope_key=%s AND input_revision<>%s AND state IN ('pending','running') RETURNING id''',
                (job_id,stage,scope_key,revision)).fetchall()
            for row in cancelled:
                self._end_attempt(conn,row['id'],'cancelled','input_changed')
            row = conn.execute('''INSERT INTO dd_agent.stage_jobs
                (id,job_id,project_id,document_id,stage,scope,scope_key,input_revision,dependencies,depends_on,run_epoch,max_attempts)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (job_id,stage,scope_key,input_revision,run_epoch) DO UPDATE SET id=dd_agent.stage_jobs.id RETURNING *''',
                (uuid4(),job_id,parent['project_id'],parent['document_id'],stage,Jsonb(scope),scope_key,revision,
                 Jsonb(deps),predecessors,parent['run_epoch'],max_attempts)).fetchone()
            return dict(row,reused=False)

    def seed(self, job_id, *, conn=None):
        """Transactional enqueue adapter; preflight does not invoke a model or parser."""
        with self._scope(conn) as conn:
            parent = self._parent(conn,job_id,exclusive=True)
            doc = conn.execute('SELECT sha256 FROM dd_agent.documents WHERE id=%s', (parent['document_id'],)).fetchone()
            from .reading import PREFLIGHT_PARSER
            return self.ensure(job_id,'preflight',{},source_revision=doc['sha256'],policy_version=PREFLIGHT_PARSER,conn=conn)

    @classmethod
    def invalidate_older(cls, conn, job_id, run_epoch):
        """Caller holds the parent row during explicit resume; never reclaim by age."""
        rows = conn.execute('''UPDATE dd_agent.stage_jobs SET state='cancelled',error_code='old_epoch',
            updated_at=clock_timestamp() WHERE job_id=%s AND run_epoch<%s
            AND state IN ('pending','running') RETURNING id''',(job_id,run_epoch)).fetchall()
        for row in rows:
            cls._end_attempt(conn,row['id'],'cancelled','old_epoch')

    @timed('stage.reconcile')
    def reconcile(self, job_id, policy_versions, limit=100, bind_vision=False):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError('每批对账事件须在 1–100 之间。')
        with self.database.connect() as conn:
            parent = self._parent(conn,job_id,exclusive=True)
            # Never advance a sequence watermark over uncommitted lower-id events.
            events = conn.execute('''SELECT e.* FROM dd_agent.page_outbox e WHERE e.document_id=%s
                AND NOT EXISTS (SELECT 1 FROM dd_agent.stage_deliveries d
                    WHERE d.job_id=%s AND d.run_epoch=%s AND d.event_id=e.id)
                ORDER BY e.id LIMIT %s''',
                (parent['document_id'],job_id,parent['run_epoch'],limit)).fetchall()
            scanned=created=0; cursor=parent['stage_cursor']; waiting=False; tasks=[]
            for event in events:
                policy = policy_versions.get(event['stage'])
                if not policy:
                    waiting=True; break  # Never skip an event because the consumer has no actual model policy yet.
                dep = dict(page_number=event['page_number'],view='raw' if event['stage']=='vision' else 'effective',revision=event['source_revision'])
                if self._sources(conn,parent['document_id'],[dep]):
                    predecessors=[]; eligible=True
                    if bind_vision and event['stage']=='embedding':
                        current=conn.execute('''SELECT e.raw_revision,m.profile_version FROM dd_agent.effective_document_pages e
                            JOIN LATERAL (SELECT profile_version FROM dd_agent.machine_pages
                                WHERE document_id=e.document_id AND page_number=e.page_number ORDER BY created_at DESC,id DESC LIMIT 1) m ON true
                            WHERE e.document_id=%s AND e.page_number=%s''',(parent['document_id'],event['page_number'])).fetchone()
                        eligible=bool(current and current['profile_version']==policy_versions.get('vision'))
                        if eligible:
                            raw_dep=dict(page_number=event['page_number'],view='raw',revision=current['raw_revision'])
                            upstream=self.ensure(job_id,'vision',{'pages':[event['page_number']]},source_revision=current['raw_revision'],
                                policy_version=policy_versions['vision'],dependencies=[raw_dep],conn=conn)
                            predecessors=[upstream['id']]
                    if eligible:
                        task=self.ensure(job_id,event['stage'],{'pages':[event['page_number']]},source_revision=event['source_revision'],
                            policy_version=policy,dependencies=[dep],depends_on=predecessors,conn=conn)
                        tasks.append(task);created+=1
                conn.execute('INSERT INTO dd_agent.stage_deliveries(job_id,run_epoch,event_id) VALUES (%s,%s,%s)',
                    (job_id,parent['run_epoch'],event['id']))
                scanned+=1; cursor=max(cursor,event['id'])
            conn.execute('UPDATE dd_agent.processing_jobs SET stage_cursor=%s WHERE id=%s', (cursor,job_id))
            return dict(scanned=scanned,created=created,cursor=cursor,waiting_for_policy=waiting,has_more=len(events)==limit,tasks=tasks)

    @timed('stage.claim')
    def claim(self, job_id, stage=None, *, task_id=None):
        if stage is not None and stage not in STAGES:
            raise ValueError('阶段无效。')
        if task_id is not None:task_id=UUID(str(task_id))
        with self.database.connect() as conn:
            parent = self._parent(conn,job_id)
            if parent['status'] not in ('queued','running') or (stage and stage in parent.get('paused_stages',[])):
                return None
            # Bounded stale cleanup; a second call can continue if 100 obsolete tasks were found.
            for _ in range(100):
                task = conn.execute('''SELECT s.* FROM dd_agent.stage_jobs s WHERE s.job_id=%s AND s.run_epoch=%s
                    AND s.state='pending' AND s.available_at<=clock_timestamp() AND (%s::text IS NULL OR s.stage=%s)
                    AND (%s::uuid IS NULL OR s.id=%s)
                    AND NOT (s.stage=ANY(%s))
                    AND NOT EXISTS (SELECT 1 FROM unnest(s.depends_on) predecessor
                        LEFT JOIN dd_agent.stage_jobs d ON d.id=predecessor WHERE d.state IS DISTINCT FROM 'succeeded')
                    ORDER BY s.available_at,s.id LIMIT 1 FOR UPDATE OF s SKIP LOCKED''',
                    (job_id,parent['run_epoch'],stage,stage,task_id,task_id,parent.get('paused_stages',[]))).fetchone()
                if task is None:
                    return None
                if not self._sources(conn,parent['document_id'],task['dependencies']):
                    conn.execute("UPDATE dd_agent.stage_jobs SET state='cancelled',error_code='source_changed',updated_at=clock_timestamp() WHERE id=%s", (task['id'],))
                    continue
                owner=uuid4()
                task = conn.execute('''UPDATE dd_agent.stage_jobs SET state='running',owner_token=%s,attempts=attempts+1,
                    updated_at=clock_timestamp() WHERE id=%s RETURNING *''', (owner,task['id'])).fetchone()
                conn.execute('INSERT INTO dd_agent.stage_attempts(stage_job_id,attempt,owner_token) VALUES (%s,%s,%s)',
                    (task['id'],task['attempts'],owner))
                return task
            return None

    @timed('stage.complete')
    def complete(self, task_id, owner_token, *, publish=None, result_ref=''):
        if not isinstance(result_ref,str) or len(result_ref)>512:
            raise ValueError('成果引用过长。')
        with self.database.connect() as conn:
            snapshot = conn.execute('SELECT * FROM dd_agent.stage_jobs WHERE id=%s', (task_id,)).fetchone()
            if snapshot is None:
                return False
            parent = self._parent(conn,snapshot['job_id'])
            if parent['run_epoch']!=snapshot['run_epoch'] or parent['status'] not in ('queued','running','pause_requested'):
                return False
            sources_valid = self._sources(conn,parent['document_id'],snapshot['dependencies'],lock=True)
            task = conn.execute('SELECT * FROM dd_agent.stage_jobs WHERE id=%s FOR UPDATE', (task_id,)).fetchone()
            if task['state']!='running' or task['owner_token']!=UUID(str(owner_token)):
                return False
            if not sources_valid:
                conn.execute("UPDATE dd_agent.stage_jobs SET state='cancelled',error_code='source_changed',updated_at=clock_timestamp() WHERE id=%s", (task_id,))
                self._end_attempt(conn,task_id,'cancelled','source_changed')
                return False
            # SQL-only, short publication. Exception rolls back both the artifact and task completion.
            if publish is not None:
                publish(conn)
            conn.execute("UPDATE dd_agent.stage_jobs SET state='succeeded',result_ref=%s,error_code='',updated_at=clock_timestamp() WHERE id=%s", (result_ref,task_id))
            self._end_attempt(conn,task_id,'succeeded')
            return True

    @contextmanager
    def parent_scope(self, job_id, run_epoch):
        """Fence preflight metadata activation too; an old parser must not switch the reading pointer."""
        from .stage_publication import StageSuperseded
        with self.database.connect() as conn:
            parent=self._parent(conn,job_id)
            if parent['run_epoch']!=run_epoch or parent['status'] not in ('queued','running','pause_requested'):
                raise StageSuperseded('父任务执行代次已失效。')
            yield conn

    @contextmanager
    def publish_scope(self, task_id, owner_token, *, final=True, success=True):
        from .stage_publication import StageSuperseded
        with self.database.connect() as conn:
            snapshot=conn.execute('SELECT * FROM dd_agent.stage_jobs WHERE id=%s',(task_id,)).fetchone()
            if snapshot is None: raise StageSuperseded('阶段不存在。')
            parent=self._parent(conn,snapshot['job_id'])
            if parent['run_epoch']!=snapshot['run_epoch'] or parent['status'] not in ('queued','running','pause_requested'):
                raise StageSuperseded('执行代次已失效。')
            if not self._sources(conn,parent['document_id'],snapshot['dependencies'],lock=True):
                raise StageSuperseded('输入来源已失效。')
            if snapshot['stage']=='reading':
                conn.execute('SELECT document_id FROM dd_agent.document_readings WHERE document_id=%s FOR UPDATE',(parent['document_id'],)).fetchone()
            task=conn.execute('SELECT * FROM dd_agent.stage_jobs WHERE id=%s FOR UPDATE',(task_id,)).fetchone()
            if task['state']!='running' or task['owner_token']!=UUID(str(owner_token)):
                raise StageSuperseded('执行者已失效。')
            yield conn
            if final:
                conn.execute('''UPDATE dd_agent.stage_jobs SET state=%s,error_code=%s,
                    failures=failures+%s,updated_at=clock_timestamp() WHERE id=%s''',
                    ('succeeded' if success else 'failed','' if success else 'page_failed',int(not success),task_id))
                self._end_attempt(conn,task_id,'succeeded' if success else 'permanent','' if success else 'page_failed')

    def park(self, task_id, owner_token, *, reason='paused', retry_after=0):
        """Pause or resource contention retires the claim without spending failure budget."""
        if reason not in ('paused','deferred') or not 0<=retry_after<=30:raise ValueError('Invalid stage parking')
        with self.database.connect() as conn:
            snapshot=conn.execute('SELECT job_id FROM dd_agent.stage_jobs WHERE id=%s',(task_id,)).fetchone()
            if not snapshot: return False
            parent=self._parent(conn,snapshot['job_id'])
            code='model_busy' if reason=='deferred' else ''
            row=conn.execute('''UPDATE dd_agent.stage_jobs SET state='pending',owner_token=NULL,error_code=%s,
                available_at=clock_timestamp()+(%s * interval '1 second'),updated_at=clock_timestamp()
                WHERE id=%s AND owner_token=%s AND run_epoch=%s AND state='running' RETURNING id''',
                (code,retry_after,task_id,owner_token,parent['run_epoch'])).fetchone()
            if row: self._end_attempt(conn,task_id,reason,code)
            return bool(row)

    @timed('stage.fail')
    def fail(self, task_id, owner_token, *, category, error_code, retry_after=1):
        if category not in ('transient','permanent','stale') or not 0 <= retry_after <= 300:
            raise ValueError('失败分类或重试等待无效。')
        if not isinstance(error_code,str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}',error_code):
            error_code='stage_failed'
        with self.database.connect() as conn:
            snapshot = conn.execute('SELECT job_id FROM dd_agent.stage_jobs WHERE id=%s',(task_id,)).fetchone()
            if snapshot is None:
                return False
            parent = self._parent(conn,snapshot['job_id'])
            task = conn.execute('SELECT * FROM dd_agent.stage_jobs WHERE id=%s FOR UPDATE',(task_id,)).fetchone()
            if task['state']!='running' or task['owner_token']!=UUID(str(owner_token)) or task['run_epoch']!=parent['run_epoch'] or parent['status'] not in ('queued','running','pause_requested'):
                return False
            state = 'cancelled' if category=='stale' else 'pending' if category=='transient' and task['failures']+1<task['max_attempts'] else 'failed'
            conn.execute('''UPDATE dd_agent.stage_jobs SET state=%s,owner_token=NULL,error_code=%s,failures=failures+1,
                available_at=clock_timestamp()+(%s * interval '1 second'),updated_at=clock_timestamp() WHERE id=%s''',
                (state,error_code,retry_after,task_id))
            self._end_attempt(conn,task_id,category,error_code)
            return True
