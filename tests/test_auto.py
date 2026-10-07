import json
from pathlib import Path
import socket
import struct
import time
from types import SimpleNamespace

import pytest
from aiohttp import ClientSession

from route_scope.autocapture import CaptureEngine
from route_scope.catalog import Catalog
from route_scope.discovery import LinuxDiscovery, tcp_rows
from route_scope.runtime import InstanceLock, atomic_json, runtime_state, supervisor_gone
from route_scope.server import Config, Service
from route_scope.store import Store
from route_scope.watch import decode_wsl_output, request_stop


def packet(src,dst,seq,flags,payload=b''):
    eth=b'\0'*12+b'\x08\x00'
    ip=bytearray(20);ip[0]=0x45;ip[2:4]=struct.pack('!H',40+len(payload));ip[9]=6
    ip[12:16]=ip[16:20]=socket.inet_aton('127.0.0.1')
    tcp=bytearray(20);struct.pack_into('!HHI',tcp,0,src,dst,seq);tcp[12]=0x50;tcp[13]=flags
    return eth+bytes(ip)+bytes(tcp)+payload


def fake_discovery():
    return SimpleNamespace(owners={},attribute=lambda r:None)


def test_engine_deduplicates_mirrored_packets_and_never_changes_body(tmp_path):
    store=Store(tmp_path);engine=CaptureEngine(store,fake_discovery(),'fixture')
    port,client=15722,41000
    syn=packet(client,port,100,2)
    engine.process(syn,'lo',{port});engine.process(syn,'loopback0',{port})
    engine.process(packet(port,client,500,18),'lo',{port})
    body=b'{"model":"m","reasoning":{"effort":"vendor-new"}}'
    req=b'POST /v1/responses HTTP/1.1\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body
    for iface in ('lo','loopback0'):
        engine.process(packet(client,port,101,24,req),iface,{port})
    data=b'{"model":"m","status":"completed","reasoning":{"effort":"vendor-new"}}'
    resp=b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(data)).encode()+b'\r\n\r\n'+data
    for iface in ('lo','loopback0'):
        engine.process(packet(port,client,501,24,resp),iface,{port})
    engine.process(packet(port,client,501+len(resp),17),'lo',{port})
    engine.process(packet(port,client,501,24,resp),'loopback0',{port})
    rows=store.list(full=True)
    assert len(rows)==1 and engine.stats['requests']==engine.stats['responses']==1
    assert rows[0]['request_body']==json.loads(body) and rows[0]['verdict']=='match'
    assert set(engine.interfaces)=={'lo','loopback0'}
    store.close()


def test_engine_pause_marks_capture_incomplete_without_request_failure(tmp_path):
    store=Store(tmp_path);engine=CaptureEngine(store,fake_discovery(),'fixture',capture_bodies=False)
    body=b'{"model":"m","reasoning":{"effort":"low"}}'
    req=b'POST /v1/responses HTTP/1.1\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body
    engine.process(packet(41000,15722,100,24,req),'lo',{15722})
    engine.close('capture_paused')
    row=store.list(full=True)[0]
    assert row['state']=='interrupted' and row['capture_end_reason']=='capture_paused'
    assert row['request_body'] is None
    store.close()


def test_discovery_follows_listener_process_replacement(tmp_path,monkeypatch):
    proc=tmp_path/'proc';(proc/'net').mkdir(parents=True)
    def process(pid,name,inode):
        p=proc/str(pid);(p/'fd').mkdir(parents=True)
        (p/'comm').write_text(name);(p/'stat').write_text(f'{pid} ({name}) S '+'0 '*18+'12345')
        (p/'fd/3').write_text('')
        return str(p/'fd/3'),f'socket:[{inode}]'
    links=dict([process(11,'cc-switch',101),process(12,'codex',102)])
    monkeypatch.setattr(Path,'readlink',lambda p:Path(links[str(p)]))
    (proc/'net/tcp').write_text('header\n0: 0100007F:3D6A 00000000:0000 0A 0 0 0 0 0 101\n0: 0100007F:A028 0100007F:3D6A 01 0 0 0 0 0 102\n')
    d=LinuxDiscovery(b'salt',proc=proc).scan(force=True)
    assert d.ports=={15722} and d.owners=={41000:[12]}
    links.update([process(13,'cc-switch',103)])
    (proc/'net/tcp').write_text('header\n0: 0100007F:414A 00000000:0000 0A 0 0 0 0 0 103\n')
    assert d.scan(force=True).ports=={16714}


def test_single_instance_lock_releases_on_exit(tmp_path):
    with InstanceLock(tmp_path/'lock'):
        with pytest.raises(RuntimeError):
            with InstanceLock(tmp_path/'lock'):pass
    with InstanceLock(tmp_path/'lock'):pass


def test_catalog_aggregates_without_recovering_worker_active_rows(tmp_path):
    atomic_json(tmp_path/'watch-status.json',{'worker_dirs':['windows','wsl','../secret']})
    for name in ('windows','wsl'):
        s=Store(tmp_path/name);s.save({'id':name,'started_at':name,'state':'streaming','request_body':'private'});s.close()
    catalog=Catalog(tmp_path)
    assert len(catalog.list())==2 and all(r['state']=='streaming' for r in catalog.list())
    assert 'request_body' not in catalog.list()[0]
    assert catalog.get('wsl')['request_body']=='private'
    assert len(catalog.databases())==2


async def test_watch_pause_resume_controls_only_own_marker(tmp_path,unused_tcp_port_factory):
    status={'run_id':'owned-run','state':'running','updated_epoch':time.time(),'worker_dirs':['wsl']}
    atomic_json(tmp_path/'watch-status.json',status)
    atomic_json(tmp_path/'wsl/capture-status.json',{'updated_epoch':time.time(),'state':'waiting_traffic'})
    config=Config(data_dir=str(tmp_path),proxy_port=unused_tcp_port_factory(),dashboard_port=unused_tcp_port_factory())
    service=Service(config,viewer=True,store_override=Catalog(tmp_path))
    await service.start()
    try:
        async with ClientSession() as client:
            url=f'http://127.0.0.1:{config.dashboard_port}'
            async with client.post(url+'/api/watch/control',json={'action':'pause'}) as r:
                assert r.status==200 and (await r.json())['paused']
            assert runtime_state(tmp_path)['paused'] is True
            async with client.post(url+'/api/watch/control',json={'action':'resume'}) as r:
                assert r.status==200
            assert runtime_state(tmp_path)['paused'] is False
            async with client.post(url+'/api/watch/control',json={'action':'pause'},headers={'Origin':'https://evil.invalid'}) as r:
                assert r.status==403
            assert request_stop(tmp_path)['run_id']=='owned-run'
            async with client.post(url+'/api/watch/control',json={'action':'resume'}) as r:
                assert r.status==409
    finally:await service.close()


def test_runtime_detects_stale_heartbeats_and_wsl_encoding(tmp_path):
    atomic_json(tmp_path/'watch-status.json',{'updated_epoch':0,'worker_dirs':['wsl']})
    assert runtime_state(tmp_path)['stale']
    assert decode_wsl_output('Ubuntu\r\n'.encode('utf-16-le'))=='Ubuntu'


def test_worker_lease_stops_orphans_without_signaling_user_processes():
    status={'run_id':'same','state':'running','updated_epoch':100}
    assert not supervisor_gone(status,'same',now=120)
    assert supervisor_gone(status,'new',now=120)
    assert supervisor_gone(status,'same',now=191)
    assert supervisor_gone({**status,'state':'stopped'},'same',now=120)


def test_existing_capture_database_migrates_to_small_summaries(tmp_path):
    import sqlite3
    record={'id':'old','started_at':'now','state':'completed','request_body':'x'*100000}
    with sqlite3.connect(tmp_path/'captures.db') as db:
        db.execute('create table captures (id text primary key, started text, data text)')
        db.execute('insert into captures values (?,?,?)',('old','now',json.dumps(record)))
    store=Store(tmp_path)
    assert store.get('old')==record
    assert 'request_body' not in store.list()[0]
    assert len(store.db.execute('select summary from captures').fetchone()[0])<200
    store.close()
