"""有界普通连接池与独立租约连接。连接池仅在明确的应用／CLI 生命周期内启用。"""
from contextlib import contextmanager, ExitStack
from threading import Lock

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolClosed, PoolTimeout, TooManyRequests

from .performance import count, span, timed


class DatabaseBusy(psycopg.OperationalError):
    """对外使用固定信息，不传播连接串或底层连接错误文本。"""


class MeasuredCursor(psycopg.Cursor):
    @timed('sql.execute')
    def execute(self, *args, **kwargs):
        count('sql.calls')
        return super().execute(*args, **kwargs)

    @timed('sql.fetch')
    def fetchall(self):
        rows = super().fetchall(); count('sql.fetched_rows', len(rows))
        return rows

    @timed('sql.fetch')
    def fetchone(self):
        row = super().fetchone(); count('sql.fetched_rows', int(row is not None))
        return row


@timed('db.connect')
def open_connection(conninfo, *, lease=False):
    if not conninfo: raise psycopg.OperationalError('Database not configured')
    count('db.connections')
    if lease: count('db.lease.connections')
    return psycopg.connect(conninfo, connect_timeout=3, row_factory=dict_row,
                           cursor_factory=MeasuredCursor,
                           application_name='dd-agent-lease' if lease else 'dd-agent')


def _reset_connection(conn):
    # 租约不走池；普通借用意外留下的 session 锁／设置也不能污染下一请求。
    # autocommit 避免归还清理本身留下 idle-in-transaction。
    conn.autocommit = True
    try:
        conn.execute('SELECT pg_advisory_unlock_all()')
        conn.execute('RESET ALL')
        conn.execute('RESET ROLE')
    finally:
        conn.autocommit = False


class ConnectionProvider:
    def __init__(self, conninfo, *, max_size=4, timeout=3., max_waiting=8):
        if not isinstance(max_size, int) or max_size < 1 or timeout <= 0 or max_waiting < 1:
            raise ValueError('连接池容量、等待上限和超时必须为正数。')
        self._conninfo = conninfo
        self.max_size, self.timeout, self.max_waiting = max_size, timeout, max_waiting
        self._lock, self._pool, self._active, self._last_stats = Lock(), None, False, {}

    @property
    def active(self):
        with self._lock: return self._active

    def start(self):
        # 不连接数据库；未配置／不可达时应用仍可启动诊断页。
        with self._lock: self._active = True
        return self

    def _get_pool(self):
        with self._lock:
            if not self._active: raise DatabaseBusy('数据库连接作用域已关闭。')
            if not self._conninfo: raise psycopg.OperationalError('Database not configured')
            if self._pool is None:
                self._pool = ConnectionPool(self._conninfo, min_size=0, max_size=self.max_size,
                    timeout=self.timeout, max_waiting=self.max_waiting, open=False,
                    kwargs=dict(connect_timeout=3, row_factory=dict_row, cursor_factory=MeasuredCursor,
                                application_name='dd-agent'),
                    check=ConnectionPool.check_connection, reset=_reset_connection,
                    num_workers=1, reconnect_timeout=3, name='dd-agent')
                self._pool.open(wait=False)
            return self._pool

    @contextmanager
    def connection(self):
        pool = self._get_pool()
        # ExitStack 在 scope 计时内完成正常提交／异常回滚及归还。
        with span('db.scope'), ExitStack() as stack:
            try:
                with span('db.pool.acquire'):
                    conn = stack.enter_context(pool.connection(timeout=self.timeout))
            except (PoolClosed, PoolTimeout, TooManyRequests):
                count('db.pool.busy')
                raise DatabaseBusy('数据库连接繁忙或暂不可用，请稍后重试。') from None
            count('db.pool.checkouts')
            yield conn

    def stats(self):
        with self._lock:
            return {**(self._pool.get_stats() if self._pool else self._last_stats), 'active': self._active}

    def close(self):
        with self._lock:
            self._active = False
            pool, self._pool = self._pool, None
            if pool: self._last_stats = pool.get_stats()
        if pool: pool.close(timeout=5.)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
