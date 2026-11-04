"""Independent process for lease/crash tests; only synthetic test databases."""
import json
from pathlib import Path
import sys

import psycopg

from dd_agent.config import Settings
from dd_agent.pipeline import JobManager


def main():
    manifest=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    settings=Settings(database_url=manifest['database_url'],data_dir=Path(manifest['data_dir']))
    if manifest['mode']=='hold':
        with psycopg.connect(manifest['database_url']) as conn:
            conn.execute('SELECT pg_advisory_lock(74332103)')
            conn.execute("UPDATE dd_agent.processing_jobs SET status='running',updated_at=clock_timestamp()-interval '2 days' WHERE id=%s",(manifest['job_id'],))
            conn.commit()
            print('READY',flush=True)
            sys.stdin.readline()
        return
    from dd_agent.retrieval import EvidenceService
    from test_retrieval import TinyModels
    class Work:
        def run(self,project_id,document_id,pages,checkpoint=None):
            engine=EvidenceService(settings,models=TinyModels())
            result=engine.index(project_id,document_id,pages,checkpoint=checkpoint)
            with Path(manifest['calls']).open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(result)+'\n')
            return dict(reading={},automatic={'failed_pages':[]},index=result)
    print('READY',flush=True)
    sys.stdin.readline()
    JobManager(settings,pipeline_factory=Work).drain()


if __name__=='__main__':main()
