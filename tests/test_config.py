import json
import sqlite3

import pytest

from route_scope.ccswitch import change, inspect, restore
from route_scope.store import Store


def fixture_db(path):
    config={'auth':{'OPENAI_API_KEY':'secret'},'config':'model_provider = "custom"\nmodel = "m"\n[model_providers.other]\nbase_url = "https://other.invalid/v1"\n[model_providers.custom]\nbase_url = "https://anyrouter.top/v1" # keep\nwire_api = "responses"\n'}
    with sqlite3.connect(path) as db:
        db.execute("create table providers(id text,app_type text,name text,settings_config text)")
        db.execute("insert into providers values ('default','codex','anyrouter',?)",(json.dumps(config),))
    return config


def test_connection_preview_apply_restore_preserves_other_edits(tmp_path):
    path=tmp_path/'cc.db';receipt=tmp_path/'receipt.json';config=fixture_db(path)
    change(path,'default','http://127.0.0.1:15723/v1',receipt)
    assert not receipt.exists() and inspect(path)[0]['base_url']=='https://anyrouter.top/v1'
    change(path,'default','http://127.0.0.1:15723/v1',receipt,True)
    assert 'secret' not in receipt.read_text()
    with sqlite3.connect(path) as db:
        value=json.loads(db.execute('select settings_config from providers').fetchone()[0])
        value['new_setting']='preserve me'
        db.execute('update providers set settings_config=?',(json.dumps(value),))
    restore(receipt,True)
    with sqlite3.connect(path) as db:
        value=json.loads(db.execute('select settings_config from providers').fetchone()[0])
    assert value['config']==config['config'] and value['new_setting']=='preserve me' and value['auth']==config['auth']
    assert restore(receipt,True)['status']=='already_restored'


def test_restore_refuses_changed_endpoint(tmp_path):
    path=tmp_path/'cc.db';receipt=tmp_path/'receipt.json';fixture_db(path)
    change(path,'default','http://127.0.0.1:15723/v1',receipt,True)
    with sqlite3.connect(path) as db:
        value=db.execute('select settings_config from providers').fetchone()[0].replace('127.0.0.1:15723','127.0.0.1:12345')
        db.execute('update providers set settings_config=?',(value,))
    with pytest.raises(ValueError,match='预期不同'):
        restore(receipt,True)


def test_retention_and_crash_recovery(tmp_path):
    store=Store(tmp_path,2)
    salt=store.salt
    for i in range(3):
        store.save({'id':str(i),'started_at':str(i),'state':'streaming'})
    assert len(store.list())==2 and store.get('0') is None
    store.close()
    store=Store(tmp_path,2)
    assert store.salt==salt and all(x['state']=='interrupted' for x in store.list())
    store.close()


def test_viewer_does_not_interrupt_active_capture(tmp_path):
    writer=Store(tmp_path)
    writer.save({'id':'1','started_at':'now','state':'streaming'})
    viewer=Store(tmp_path,recover=False)
    assert viewer.get('1')['state']=='streaming'
    viewer.close();writer.close()
