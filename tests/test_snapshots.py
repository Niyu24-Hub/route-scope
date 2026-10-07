import json
from pathlib import Path
import sqlite3

import pytest

from route_scope.catalog import Catalog
from route_scope.runtime import atomic_json
from route_scope.snapshots import SnapshotStore, migrate_database


def record(ident,body='private body'):
    return {'id':ident,'started_at':ident,'state':'completed','request_body':body,'requested':{'present':True,'value':'xhigh'}}


def test_cross_os_reader_uses_snapshot_without_opening_sqlite(tmp_path,monkeypatch):
    root=tmp_path/'shared';native=tmp_path/'native'
    atomic_json(root/'watch-status.json',{'worker_dirs':['wsl-test']})
    store=SnapshotStore(root/'wsl-test',database_dir=native)
    store.save(record('a'));assert store.publish_snapshot()
    assert not (root/'wsl-test/captures.db').exists()
    monkeypatch.setattr(sqlite3,'connect',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Cross-OS SQLite must not be opened')))
    catalog=Catalog(root)
    assert [r['id'] for r in catalog.list()]==['a']
    assert 'request_body' not in catalog.list()[0]
    assert catalog.get('a')['request_body']=='private body'
    assert catalog.list(full=True)[0]['request_body']=='private body'
    store.close()


def test_corrupt_snapshot_reports_error_and_keeps_last_good_list(tmp_path):
    atomic_json(tmp_path/'watch-status.json',{'worker_dirs':['source']})
    store=SnapshotStore(tmp_path/'source');store.save(record('a'));store.publish_snapshot()
    catalog=Catalog(tmp_path)
    assert len(catalog.list())==1
    (tmp_path/'source/capture-index.json').write_text('{broken')
    assert len(catalog.list())==1
    assert catalog.read_errors['source']['using_cached_records']
    catalog=Catalog(tmp_path)
    assert catalog.list()==[] and catalog.read_errors
    store.close()


def test_failed_record_publication_does_not_replace_valid_index(tmp_path,monkeypatch):
    from route_scope import snapshots
    store=SnapshotStore(tmp_path)
    store.save(record('a'));assert store.publish_snapshot()
    before=(tmp_path/'capture-index.json').read_bytes()
    store.save(record('b'))
    real=snapshots.atomic_text
    def fail(path,text):
        if Path(path).name=='b.json':raise PermissionError('reader holds old file')
        real(path,text)
    monkeypatch.setattr(snapshots,'atomic_text',fail)
    assert not store.publish_snapshot()
    assert (tmp_path/'capture-index.json').read_bytes()==before
    assert store.get('b') and 'b' in store.dirty
    monkeypatch.setattr(snapshots,'atomic_text',real)
    assert store.publish_snapshot()
    assert len(json.loads((tmp_path/'capture-index.json').read_text())['items'])==2
    store.close()


def test_native_migration_preserves_history_and_original(tmp_path):
    shared=tmp_path/'shared';native=tmp_path/'native';shared.mkdir()
    with sqlite3.connect(shared/'captures.db') as db:
        db.execute('create table captures (id text primary key, started text, data text)')
        db.execute('insert into captures values (?,?,?)',('old','old',json.dumps(record('old'))))
    migrate_database(shared,native)
    assert (shared/'captures.db').exists()
    store=SnapshotStore(shared,database_dir=native)
    assert store.get('old')==record('old')
    assert json.loads((shared/'storage-migration.json').read_text())['records_preserved']==1
    store.close()


def test_retention_and_unsafe_capture_ids(tmp_path):
    root=tmp_path/'shared';atomic_json(root/'watch-status.json',{'worker_dirs':['source']})
    store=SnapshotStore(root/'source',retention=1)
    store.save(record('a'));store.publish_snapshot()
    store.save(record('b'));store.publish_snapshot()
    assert not (root/'source/records/a.json').exists()
    catalog=Catalog(root)
    assert catalog.get('../secret') is None
    assert catalog.get('b')['id']=='b'
    store.close()


def test_sqlite_failure_is_not_silently_reported_as_empty(tmp_path):
    atomic_json(tmp_path/'watch-status.json',{'worker_dirs':['source']})
    (tmp_path/'source').mkdir();(tmp_path/'source/captures.db').write_bytes(b'not a database')
    catalog=Catalog(tmp_path)
    assert catalog.list()==[]
    assert catalog.read_errors['source']['error']=='DatabaseError'


async def test_viewer_auto_selects_snapshot_and_exposes_read_errors(tmp_path,unused_tcp_port_factory):
    from aiohttp import ClientSession
    from route_scope.server import Config,Service
    shared=tmp_path/'shared';native=tmp_path/'native'
    store=SnapshotStore(shared,database_dir=native);store.save(record('a'));store.publish_snapshot()
    service=Service(Config(data_dir=str(shared),proxy_port=unused_tcp_port_factory(),dashboard_port=unused_tcp_port_factory()),viewer=True)
    assert isinstance(service.store,Catalog)
    await service.start()
    try:
        async with ClientSession() as client:
            url=f'http://127.0.0.1:{service.config.dashboard_port}'
            async with client.get(url+'/api/captures') as r:
                value=await r.json();assert len(value['items'])==1 and not value['errors']
            (shared/'capture-index.json').write_text('broken',encoding='utf-8')
            async with client.get(url+'/api/captures') as r:
                value=await r.json();assert len(value['items'])==1 and value['errors']
            async with client.get(url+'/api/captures/a') as r:assert r.status==503
            async with client.get(url+'/api/export') as r:assert r.status==503
    finally:
        store.close();await service.close()
