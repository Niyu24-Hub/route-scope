"""Read snapshots across OS boundaries; never open a live WSL WAL from Windows."""
import json
from contextlib import closing
import os
from pathlib import Path
import sqlite3

from .runtime import read_json, runtime_state
from .snapshots import valid_id
from .store import compact


class Catalog:
    def __init__(self,directory,retention=1000):
        self.directory=Path(directory);self.retention=retention
        self.read_errors={};self.cache={};self.snapshots={}

    def databases(self):
        if (self.directory/'storage.json').exists() or (self.directory/'capture-index.json').exists():
            return [self.directory/'captures.db']
        names=read_json(self.directory/'watch-status.json').get('worker_dirs',[])
        return [self.directory/n/'captures.db' for n in names if Path(n).name==n and n not in ('.','..')]

    def snapshot_source(self,path):
        folder=path.parent
        return (folder/'storage.json').exists() or (folder/'capture-index.json').exists() or (os.name=='nt' and folder.name.startswith('wsl-'))

    def error(self,path,exc):
        self.read_errors[path.parent.name]={'source':path.parent.name,'error':type(exc).__name__,
            'message':'采集数据读取失败，不能视为没有请求；请检查来源快照发布状态。',
            'using_cached_records':bool(self.cache.get(path.parent.name))}

    def index(self,path):
        value=json.loads((path.parent/'capture-index.json').read_text(encoding='utf-8'))
        if not isinstance(value,dict) or value.get('schema')!=1 or not isinstance(value.get('items'),list):raise ValueError('Invalid snapshot')
        for r in value['items']:
            if not isinstance(r,dict) or not valid_id(r.get('id')) or not isinstance(r.get('started_at'),str):raise ValueError('Invalid snapshot record')
        self.snapshots[path.parent.name]=value.get('published_epoch')
        return value['items']

    def list(self,limit=1000,full=False):
        self.read_errors={};result=[]
        for path in self.databases():
            try:
                if self.snapshot_source(path):
                    rows=self.index(path)
                    if full:
                        rows=[json.loads((path.parent/'records'/(r['id']+'.json')).read_text(encoding='utf-8')) for r in rows[:limit]]
                else:
                    if not path.exists():continue
                    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
                        cols={r[1] for r in db.execute('pragma table_info(captures)')}
                        column='coalesce(summary,data)' if not full and 'summary' in cols else 'data'
                        rows=[json.loads(r[0]) for r in db.execute(f'select {column} from captures order by started desc limit ?',(min(limit,self.retention),))]
                if not full:
                    rows=[compact(r) for r in rows]
                    self.cache[path.parent.name]=rows
                result.extend(rows)
            except (sqlite3.Error,OSError,ValueError,KeyError,TypeError) as exc:
                self.error(path,exc)
                if not full:result.extend(self.cache.get(path.parent.name,[]))
        result.sort(key=lambda r:r['started_at'],reverse=True)
        return result[:min(limit,self.retention)]

    def get(self,capture_id):
        self.read_errors={}
        if not valid_id(capture_id):return None
        for path in self.databases():
            try:
                if self.snapshot_source(path):
                    if not any(r['id']==capture_id for r in self.index(path)):continue
                    value=json.loads((path.parent/'records'/(capture_id+'.json')).read_text(encoding='utf-8'))
                    if value.get('id')!=capture_id:raise ValueError('Mismatched snapshot id')
                    return value
                if not path.exists():continue
                with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
                    row=db.execute('select data from captures where id=?',(capture_id,)).fetchone()
                    if row:return json.loads(row[0])
            except (sqlite3.Error,OSError,ValueError,AttributeError) as exc:self.error(path,exc)
        return None

    def runtime_status(self):
        return runtime_state(self.directory)

    def close(self):pass
