"""纯模型输出的内存缓存；不保存正文、材料对象或整份搜索响应。

缓存键由调用方绑定模型版本和实际输入摘要。TTL 仅用于容量回收，
不负责判断来源是否仍有效；来源必须每次经数据库重新校验。
"""
from collections import OrderedDict
import json
from threading import Lock
from time import monotonic


class ModelResults:
    def __init__(self,*,max_entries=64,max_bytes=16*1024*1024,ttl=300.):
        if max_entries<1 or max_bytes<1 or ttl<=0:raise ValueError('Invalid model cache budget')
        self.limit,self.byte_limit,self.ttl=max_entries,max_bytes,ttl
        self.rows=OrderedDict();self.bytes=0;self.lock=Lock()

    def _remove(self,key):
        _,value=self.rows.pop(key);self.bytes-=len(value)

    def get(self,key):
        with self.lock:
            row=self.rows.get(key)
            if row is None:return None
            expires,value=row
            if expires<=monotonic():self._remove(key);return None
            self.rows.move_to_end(key)
        # Decode after releasing the lock; callers cannot mutate cached lists.
        return json.loads(value)

    def put(self,key,value):
        data=json.dumps(value,allow_nan=False,separators=(',',':')).encode('utf-8')
        if len(data)>self.byte_limit:return
        with self.lock:
            now=monotonic()
            for old,(expires,_) in list(self.rows.items()):
                if expires<=now:self._remove(old)
            if key in self.rows:self._remove(key)
            self.rows[key]=(now+self.ttl,data);self.bytes+=len(data)
            while len(self.rows)>self.limit or self.bytes>self.byte_limit:self._remove(next(iter(self.rows)))
