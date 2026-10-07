import asyncio
import gzip
import hashlib
import json

import pytest
from aiohttp import ClientSession, web

from route_scope.server import Config, Service
from test_evidence import frame, response


@pytest.fixture
async def rig(tmp_path, unused_tcp_port_factory):
    upstream_port, proxy_port, ui_port = [unused_tcp_port_factory() for _ in range(3)]
    seen=[]
    first_sent=asyncio.Event()
    release=asyncio.Event()
    async def upstream(request):
        body=await request.read()
        seen.append({"body":body,"headers":dict(request.headers),"path":request.raw_path})
        if request.path.endswith("/ws"):
            ws=web.WebSocketResponse()
            await ws.prepare(request)
            async for message in ws:
                payload=json.loads(message.data)
                await ws.send_json({"type":"response.created","response":response(payload["reasoning"]["effort"])})
                await ws.send_json({"type":"response.completed","response":response(payload["reasoning"]["effort"])})
            return ws
        data=frame("response.created",response("high"))+frame("response.completed",response("low"))
        if request.path.endswith("/compressed"):
            return web.Response(body=gzip.compress(data),headers={"Content-Encoding":"gzip","Content-Type":"text/event-stream","x-request-id":"real-id"})
        if request.path.endswith("/slow"):
            r=web.StreamResponse(headers={"Content-Type":"text/event-stream"})
            await r.prepare(request)
            await r.write(frame("response.created",response("high")))
            first_sent.set()
            await release.wait()
            await r.write(frame("response.completed",response("high")))
            return r
        if request.path.endswith("/redirect"):
            return web.Response(status=307,headers={"Location":"http://unrelated.invalid/"})
        if request.path.endswith("/empty"):
            return web.Response(status=204)
        return web.Response(body=data,content_type="text/event-stream")
    app=web.Application()
    app.router.add_route("*","/{path:.*}",upstream)
    runner=web.AppRunner(app,auto_decompress=False)
    await runner.setup()
    await web.TCPSite(runner,"127.0.0.1",upstream_port).start()
    service=Service(Config(upstream=f"http://127.0.0.1:{upstream_port}",proxy_port=proxy_port,dashboard_port=ui_port,data_dir=str(tmp_path)))
    await service.start()
    async with ClientSession(auto_decompress=False) as client:
        yield service,client,f"http://127.0.0.1:{proxy_port}",f"http://127.0.0.1:{ui_port}",seen,first_sent,release
    release.set()
    await service.close()
    await runner.cleanup()


async def test_proxy_preserves_bytes_auth_query_and_compression(rig):
    service,client,proxy,ui,seen,*_=rig
    raw=b'{ "model":"m", "reasoning": {"effort":"max"}, "input":"secret body" }'
    async with client.post(proxy+"/v1/compressed?x=%2F&x=two",data=raw,headers={"Authorization":"Bearer test-private-token","Connection":"keep-alive, x-hop-secret","x-hop-secret":"remove"}) as r:
        data=await r.read()
        assert r.headers["Content-Encoding"] == "gzip"
        assert r.headers["x-request-id"] == "real-id"
    assert seen[0]["body"] == raw
    assert seen[0]["path"] == "/v1/compressed?x=/&x=two"  # Client URL normalization happens before proxy.
    assert seen[0]["headers"]["Authorization"] == "Bearer test-private-token"
    assert "x-hop-secret" not in seen[0]["headers"]
    record=service.store.list(full=True)[0]
    assert record["response_sha256"] == hashlib.sha256(data).hexdigest()
    assert record["verdict"] == "lower"
    async with client.get(ui+"/api/export") as r:
        exported=await r.text()
    assert "test-private-token" not in exported and "secret body" in exported


async def test_proxy_streams_first_event_before_completion(rig):
    _,client,proxy,_,_,sent,release=rig
    async with client.post(proxy+"/v1/slow",json={"model":"m","reasoning":{"effort":"high"}}) as r:
        await asyncio.wait_for(sent.wait(),1)
        first=await asyncio.wait_for(r.content.readany(),1)
        assert b"response.created" in first and b"response.completed" not in first
        release.set()
        assert b"response.completed" in await r.read()


async def test_compressed_request_is_not_rewritten(rig):
    import zstandard
    service,client,proxy,_,seen,*_=rig
    raw=zstandard.ZstdCompressor().compress(b'{"model":"m","reasoning":{"effort":"max"}}')
    async with client.post(proxy+"/v1/responses",data=raw,headers={"Content-Encoding":"zstd"}) as r:
        await r.read()
    assert seen[0]["body"] == raw
    assert service.store.list()[0]["requested"]["value"] == "max"


async def test_redirect_not_followed_and_204_preserved(rig):
    _,client,proxy,_,seen,*_=rig
    async with client.post(proxy+"/redirect",data=b"{}",allow_redirects=False) as r:
        assert r.status == 307
    assert len(seen)==1
    async with client.get(proxy+"/empty") as r:
        assert r.status==204 and await r.read()==b""


async def test_dashboard_blocks_cross_origin_and_bad_host(rig):
    _,client,proxy,ui,*_=rig
    async with client.get(ui+"/api/captures",headers={"Origin":"https://evil.invalid"}) as r:
        assert r.status==403
    async with client.get(ui+"/api/captures",headers={"Host":"evil.invalid"}) as r:
        assert r.status==403
    async with client.get(ui) as r:
        assert r.status==200 and "Route Scope" in await r.text()
    async with client.get(proxy+"/api/captures") as r:
        assert r.headers.get("Content-Type", "").startswith("text/event-stream")  # No dashboard on proxy port.


async def test_websocket_sequential_requests_each_get_a_record(rig):
    service,client,proxy,*_=rig
    async with client.ws_connect(proxy+"/ws") as ws:
        for level in ("high","xhigh"):
            await ws.send_json({"type":"response.create","model":"m","reasoning":{"effort":level}})
            assert (await ws.receive_json())["type"]=="response.created"
            assert (await ws.receive_json())["type"]=="response.completed"
    records=service.store.list()
    assert len(records)==2
    assert all(r["verdict"]=="match" for r in records)
    assert {r["requested"]["value"] for r in records}=={"high","xhigh"}


async def test_raw_custom_effort_and_model_alias_survive_proxy(rig):
    service,client,proxy,_,seen,*_=rig
    payload={"model":"gpt-6-astar", "reasoning":{"effort":"vendor-new-effort"}}
    async with client.post(proxy+"/v1/responses",json=payload) as r:
        await r.read()
    record=service.store.list()[0]
    assert record['requested']['value']=='vendor-new-effort'
    assert record['requested_model']=='gpt-6-astar'
    assert record['effort_origin']=='captured_request_body'
    assert record['verdict']=='changed'  # An unknown value has no assumed rank.
    assert json.loads(seen[0]['body'])==payload
