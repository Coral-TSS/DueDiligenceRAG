"""Lifecycle for databases issued by this test run, never application databases.

No FORCE, backend termination, fsync changes or filesystem deletion. Failed/busy
databases remain in the isolated cluster for diagnosis. The journal omits DSNs.
"""
import json
from pathlib import Path
import re
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


class IsolatedDatabase:
    def __init__(self,cluster,journal,*,node_id=None):
        self._cluster=cluster
        self._issued_name=self.name='dda_test_'+uuid4().hex
        self._journal=Path(journal);self._node_id=node_id;self._dropped=False
        # CREATE must succeed before this instance is eligible for any DROP.
        self._created=False
        with self._admin() as conn:
            conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(self.name)))
        self._created=True
        self._record('created','setup')

    @property
    def url(self):
        return make_conninfo(self._cluster,dbname=self.name)

    def _admin(self):
        return psycopg.connect(make_conninfo(self._cluster,dbname='postgres'),autocommit=True,connect_timeout=3)

    def _record(self,action,outcome,**extra):
        self._journal.parent.mkdir(parents=True,exist_ok=True)
        row=dict(database=self._issued_name,action=action,outcome=outcome,**extra)
        if self._node_id:row['node_id']=self._node_id
        with self._journal.open('a',encoding='utf-8') as stream:
            stream.write(json.dumps(row,ensure_ascii=False)+'\n')
        return action

    def finish(self,outcome,*,cluster_running=True):
        if not self._created or self.name!=self._issued_name or not re.fullmatch(r'dda_test_[0-9a-f]{32}',self.name):
            raise ValueError('Refusing cleanup of a database not issued by this test instance')
        if self._dropped:return 'dropped'
        if outcome not in ('passed','skipped'):
            return self._record('retained_failed' if outcome=='failed' else 'retained_unknown',outcome)
        if not cluster_running:return self._record('retained_cluster_closed',outcome)
        try:
            with self._admin() as conn:
                # Idle pooled sessions still count: do not disrupt an application's
                # connection lifetime just to make the reaper succeed.
                busy=conn.execute('SELECT 1 FROM pg_stat_activity WHERE datname=%s LIMIT 1',(self.name,)).fetchone()
                if busy:return self._record('retained_busy',outcome)
                conn.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(self.name)))
        except psycopg.errors.ObjectInUse:
            return self._record('retained_busy',outcome)
        except psycopg.Error as exc:
            return self._record('retained_error',outcome,error_type=type(exc).__name__)
        self._dropped=True
        return self._record('dropped',outcome)
