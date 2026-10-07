"""Publish sanitized records without sharing SQLite WAL/SHM across operating systems."""
import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import time

from .store import Store


def valid_id(value):
    return isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,80}',value) is not None


def atomic_text(path,text):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_name(path.name+f'.{os.getpid()}.tmp')
    temp.write_text(text,encoding='utf-8')
    try:
        for attempt in range(4):
            try:os.replace(temp,path);return
            except PermissionError:
                if attempt==3:raise
                time.sleep(.02)
    finally:temp.unlink(missing_ok=True)


def native_database_directory(shared):
    shared=Path(shared).resolve()
    if os.name=='nt':return shared
    root=os.environ.get('ROUTE_SCOPE_STORAGE')
    if not root:
        import pwd
        users=sorted((u for u in pwd.getpwall() if 1000<=u.pw_uid<60000 and Path(u.pw_dir).is_dir()),key=lambda u:u.pw_uid)
        home=Path(users[0].pw_dir) if users else Path.home()
        root=str(home/'.local/share/route-scope/storage')
    result=Path(root)/hashlib.sha256(str(shared).encode()).hexdigest()[:20]
    if str(result.resolve()).startswith('/mnt/'):
        raise ValueError('ROUTE_SCOPE_STORAGE 必须位于 Linux 本地文件系统，不能放在 /mnt Windows 共享盘')
    return result


def migrate_database(shared,native):
    shared,native=Path(shared).resolve(),Path(native).resolve()
    native.mkdir(parents=True,exist_ok=True)
    target=native/'captures.db';source=shared/'captures.db'
    if native==shared or target.exists() or not source.exists():return
    temp=native/('migration-'+str(os.getpid())+'.db')
    # Run on the original writer's OS. Never discard the source or its WAL.
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True,timeout=15)) as src:
        with closing(sqlite3.connect(temp)) as dst:
            src.backup(dst,pages=256,sleep=.01)
            result=dst.execute('pragma integrity_check').fetchall()
            if result!=[('ok',)]:raise RuntimeError('历史数据库完整性校验失败；原文件已保留，停止迁移')
            count=dst.execute('select count(*) from captures').fetchone()[0]
    os.replace(temp,target)
    atomic_text(shared/'storage-migration.json',json.dumps({'source':str(source),'database':str(target),
        'records_preserved':count,'integrity':'ok','migrated_epoch':time.time(),'original_retained':True},ensure_ascii=False,indent=2))


class SnapshotStore(Store):
    def __init__(self,directory,retention=1000,database_dir=None):
        self.dirty=set();self.publication_error=None;self.published_epoch=None
        super().__init__(directory,retention,database_dir=database_dir)
        self.dirty.update(row[0] for row in self.db.execute('select id from captures'))
        atomic_text(self.directory/'storage.json',json.dumps({'transport':'snapshot-v1','database_host':os.name,
            'database':str(self.database_directory/'captures.db'),'snapshot':'capture-index.json'},ensure_ascii=False))
        self.publish_snapshot(force=True)

    def save(self,record):
        super().save(record);self.dirty.add(record['id'])

    def publish_snapshot(self,force=False):
        if not force and not self.dirty:return True
        try:
            rows=self.db.execute('select id,summary from captures order by started desc limit ?',(self.retention,)).fetchall()
            ids={r[0] for r in rows}
            for capture_id in self.dirty & ids:
                if not valid_id(capture_id):raise ValueError('Invalid capture id')
                data=self.db.execute('select data from captures where id=?',(capture_id,)).fetchone()[0]
                atomic_text(self.directory/'records'/(capture_id+'.json'),data)
            now=time.time()
            atomic_text(self.directory/'capture-index.json',json.dumps({'schema':1,'published_epoch':now,
                'count':len(rows),'items':[json.loads(r[1]) for r in rows]},ensure_ascii=False))
            self.dirty.clear();self.published_epoch=now;self.publication_error=None
            for p in (self.directory/'records').glob('*.json'):
                if valid_id(p.stem) and p.stem not in ids:
                    try:p.unlink()
                    except OSError:pass
            return True
        except (OSError,sqlite3.Error,ValueError) as exc:
            self.publication_error=type(exc).__name__
            return False

    def close(self):
        self.publish_snapshot(force=True);super().close()
