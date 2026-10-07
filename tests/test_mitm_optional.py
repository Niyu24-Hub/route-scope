"""Opt-in verification against the real mitmdump engine, no external traffic."""
import asyncio
import gzip
import json
import os
from pathlib import Path
import sqlite3
import subprocess

import pytest
from aiohttp import ClientSession, web

from test_evidence import frame, response


@pytest.mark.skipif(not os.environ.get("ROUTE_SCOPE_MITMDUMP"), reason="optional real mitmproxy engine")
async def test_actual_mitmproxy_adapter(tmp_path, unused_tcp_port_factory):
    upstream_port, proxy_port = unused_tcp_port_factory(), unused_tcp_port_factory()
    raw=gzip.compress(frame('response.created',response('high'))+frame('response.completed',response('low')))
    async def upstream(request):
        assert (await request.json())['reasoning']['effort']=='max'
        return web.Response(body=raw,headers={'Content-Type':'text/event-stream','Content-Encoding':'gzip'})
    app=web.Application();app.router.add_post('/v1/responses',upstream)
    runner=web.AppRunner(app);await runner.setup();await web.TCPSite(runner,'127.0.0.1',upstream_port).start()
    root=Path(__file__).resolve().parents[1]
    log=(tmp_path/'mitm.log').open('wb')
    process=subprocess.Popen([os.environ['ROUTE_SCOPE_MITMDUMP'],'--mode',f'reverse:http://127.0.0.1:{upstream_port}@{proxy_port}',
        '--listen-host','127.0.0.1','-s',str(root/'addons/mitm_capture.py'),'--set',f'route_scope_data={tmp_path / "capture"}',
        '--set','route_scope_hosts=127.0.0.1','--set',f'confdir={tmp_path / "mitm-config"}'],stdout=log,stderr=log,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        for _ in range(100):
            if process.poll() is not None:
                pytest.fail((tmp_path/'mitm.log').read_text(errors='replace'))
            try:
                reader,writer=await asyncio.open_connection('127.0.0.1',proxy_port)
                writer.close();await writer.wait_closed();break
            except OSError:
                await asyncio.sleep(.1)
        else:
            pytest.fail('mitmproxy did not start')
        async with ClientSession(auto_decompress=False) as client:
            async with client.post(f'http://127.0.0.1:{proxy_port}/v1/responses',json={'model':'m','reasoning':{'effort':'max'}},headers={'Authorization':'Bearer private-mitm-key'}) as r:
                assert await r.read()==raw
        for _ in range(50):
            with sqlite3.connect(tmp_path/'capture/captures.db') as db:
                records=[json.loads(x[0]) for x in db.execute('select data from captures')]
            if records and records[0]['state']=='completed':
                break
            await asyncio.sleep(.05)
        assert records[0]['verdict']=='lower'
        assert records[0]['first']['value']=='high' and records[0]['final']['value']=='low'
        assert 'private-mitm-key' not in json.dumps(records)
    finally:
        process.terminate()
        await asyncio.to_thread(process.wait,10)
        log.close()
        await runner.cleanup()
