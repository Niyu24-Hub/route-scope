import asyncio
import gzip
import json

import pytest
from aiohttp import ClientSession, web

from route_scope.routing import PolicyError, RoutingGuard, stateless_text
from route_scope.server import Config, Service
from route_scope.store import Store


@pytest.fixture
async def gateway(tmp_path,unused_tcp_port_factory,monkeypatch):
    ports=[unused_tcp_port_factory() for _ in range(4)]
    calls=[[],[]];levels=['high','high'];statuses=[200,200]
    created=asyncio.Event();release=asyncio.Event();slow=[False,False]
    missing=[False,False];gzip_reply=[False,False]
    async def endpoint(request,index):
        payload=await request.json();calls[index].append((payload,dict(request.headers)))
        if statuses[index]!=200:
            return web.json_response({'error':{'message':'upstream error'}},status=statuses[index],headers={'Retry-After':'60'})
        first={'id':'response-'+str(index),'model':payload['model'],'status':'in_progress','reasoning':{'effort':payload['reasoning']['effort']}}
        final={**first,'status':'completed','reasoning':{'effort':levels[index]},'output':[{'content':[{'type':'output_text','text':'admitted answer'}]}]}
        if missing[index]:final.pop('reasoning')
        frame=lambda kind,value:('data: '+json.dumps({'type':kind,'response':value})+'\n\n').encode()
        if gzip_reply[index]:return web.Response(body=gzip.compress(frame('response.completed',final)),headers={'Content-Type':'text/event-stream','Content-Encoding':'gzip'})
        resp=web.StreamResponse(headers={'Content-Type':'text/event-stream'})
        await resp.prepare(request);await resp.write(frame('response.created',first));created.set()
        if slow[index]:await release.wait()
        await resp.write(frame('response.completed',final));await resp.write_eof()
        return resp
    runners=[]
    for index in range(2):
        app=web.Application()
        async def handle(req,i=index):return await endpoint(req,i)
        app.router.add_post('/v1/responses',handle)
        runner=web.AppRunner(app);await runner.setup();await web.TCPSite(runner,'127.0.0.1',ports[index]).start();runners.append(runner)
    monkeypatch.setenv('ROUTE_SCOPE_TEST_ALT_KEY','private-alternative-key')
    config=Config(upstream=f'http://127.0.0.1:{ports[0]}',proxy_port=ports[2],dashboard_port=ports[3],data_dir=str(tmp_path),
        guard={'models':['m'],'minimum_effort':'high','raise_effort':True,'max_attempts':2,'retry_stateless':True},
        routes=[{'name':'a','upstream':f'http://127.0.0.1:{ports[0]}'},{'name':'b','upstream':f'http://127.0.0.1:{ports[1]}','key_env':'ROUTE_SCOPE_TEST_ALT_KEY'}])
    service=Service(config);await service.start()
    async with ClientSession(auto_decompress=False) as client:
        yield {'service':service,'client':client,'url':f'http://127.0.0.1:{ports[2]}/v1/responses',
               'calls':calls,'levels':levels,'statuses':statuses,'slow':slow,'created':created,'release':release,'missing':missing,'gzip':gzip_reply}
    release.set();await service.close()
    for runner in runners:await runner.cleanup()


def payload(level='high',**kwargs):
    return {'model':'m','reasoning':{'effort':level},'input':'one stateless request','stream':True,**kwargs}


async def test_floor_is_explicit_and_preserves_original_capture(gateway):
    g=gateway;g['gzip'][0]=True
    async with g['client'].post(g['url'],json=payload('low')) as r:
        assert r.status==200 and r.headers['Content-Encoding']=='gzip'
        assert b'admitted answer' in gzip.decompress(await r.read())
    record=g['service'].store.list(full=True)[0]
    assert record['requested']['value']=='low' and record['forwarded_requested']['value']=='high'
    assert record['routing']['delivery']=='admitted' and record['routing']['rewritten']
    assert record['outbound_verdict']=='match' and record['verdict']=='changed'
    assert g['calls'][0][0][0]['reasoning']['effort']=='high'


async def test_stream_is_withheld_until_final_and_cooldown_prevents_more_calls(gateway):
    g=gateway;g['service'].guard.max_attempts=1;g['slow'][0]=True
    task=asyncio.create_task(g['client'].post(g['url'],json=payload('xhigh')))
    await asyncio.wait_for(g['created'].wait(),2)
    for _ in range(20):
        rows=g['service'].store.list()
        if rows and rows[0]['first']:break
        await asyncio.sleep(.01)
    assert rows[0]['first']['value']=='xhigh' and rows[0]['routing']['delivery']=='withheld'
    assert not task.done()
    g['release'].set()
    async with await task as r:
        assert r.status==422
        assert 'admitted answer' not in await r.text()
    # Both routes below xhigh will be quarantined on successive single attempts.
    for _ in range(2):
        async with g['client'].post(g['url'],json=payload('xhigh')) as r:assert r.status==422
    assert len(g['calls'][0])==len(g['calls'][1])==1


async def test_bounded_stateless_failover_and_secrets_redaction(gateway):
    g=gateway;g['levels'][0]='low'
    async with g['client'].post(g['url'],json=payload(),headers={'Authorization':'Bearer incoming-private-key','api-key':'incoming-private-key','x-secret-token':'another-private-key'}) as r:
        assert r.status==200
        assert b'admitted answer' in await r.read()
    assert len(g['calls'][0])==len(g['calls'][1])==1
    assert g['calls'][1][0][1]['Authorization']=='Bearer private-alternative-key'
    assert 'api-key' not in g['calls'][1][0][1] and 'x-secret-token' not in g['calls'][1][0][1]
    all_records=json.dumps(g['service'].store.list(full=True))
    assert 'private-alternative-key' not in all_records and 'incoming-private-key' not in all_records
    assert {r['routing']['delivery'] for r in g['service'].store.list()}=={'admitted','rejected'}
    records=g['service'].store.list()
    assert len({r['routing']['request_group'] for r in records})==1
    assert next(r for r in records if r['routing']['delivery']=='rejected')['routing']['client_status'] is None
    assert next(r for r in records if r['routing']['delivery']=='admitted')['routing']['client_status']==200


async def test_tool_requests_are_never_replayed_or_rerouted(gateway):
    g=gateway;g['levels'][0]='low'
    async with g['client'].post(g['url'],json=payload(tools=[{'type':'function','name':'write_file'}])) as r:assert r.status==422
    assert len(g['calls'][0])==1 and not g['calls'][1]


async def test_affinity_does_not_move_opaque_conversation_after_bad_echo(gateway):
    g=gateway;g['levels'][0]='xhigh';headers={'session_id':'same-conversation'}
    async with g['client'].post(g['url'],json=payload('xhigh'),headers=headers) as r:assert r.status==200
    g['levels'][0]='low'
    async with g['client'].post(g['url'],json=payload('xhigh',previous_response_id='response-0'),headers=headers) as r:assert r.status==422
    async with g['client'].post(g['url'],json=payload('xhigh'),headers=headers) as r:assert r.status==422
    assert len(g['calls'][0])==2 and not g['calls'][1]


async def test_rate_limit_is_returned_without_key_rotation(gateway):
    g=gateway;g['statuses'][0]=429
    async with g['client'].post(g['url'],json=payload()) as r:
        assert r.status==429 and r.headers['Retry-After']=='60'
    assert len(g['calls'][0])==1 and not g['calls'][1]


async def test_final_effort_missing_is_not_filled_from_first_echo(gateway):
    g=gateway;g['missing'][0]=True;g['service'].guard.max_attempts=1
    async with g['client'].post(g['url'],json=payload()) as r:assert r.status==422
    r=g['service'].store.list()[0]
    assert r['first']['value']=='high' and not r['final']['present']
    assert r['routing']['reason']=='effort_missing_or_unranked'


async def test_unknown_effort_and_unconfigured_model_rejected_before_upstream(gateway):
    g=gateway
    for p in (payload('unknown'),{**payload(),'model':'other'}):
        async with g['client'].post(g['url'],json=p) as r:assert r.status==400
    assert not any(g['calls'])


async def test_guard_endpoints_cannot_bypass_admission(gateway):
    g=gateway
    async with g['client'].get(g['url']) as r:assert r.status==400
    async with g['client'].post(g['url'],json=payload(),headers={'Origin':'https://unrelated.example'}) as r:assert r.status==403
    async with g['client'].post(g['url'],json=payload(),headers={'Host':'unrelated.example'}) as r:assert r.status==403
    async with g['client'].post(g['url'],data=json.dumps(payload())) as r:assert r.status==415
    assert not any(g['calls'])


async def test_unbound_opaque_context_is_not_moved_to_a_guessed_route(gateway):
    g=gateway
    async with g['client'].post(g['url'],json=payload(previous_response_id='unknown-existing-conversation')) as r:assert r.status==422
    assert not any(g['calls'])


async def test_preference_applies_to_new_sessions_but_preserves_existing_affinity(gateway):
    g=gateway;guard=g['service'].guard
    guard.prefer('b')
    async with g['client'].post(g['url'],json=payload(),headers={'session_id':'s1'}) as r:assert r.status==200
    guard.prefer('a')
    async with g['client'].post(g['url'],json=payload(),headers={'session_id':'s1'}) as r:assert r.status==200
    async with g['client'].post(g['url'],json=payload(),headers={'session_id':'s2'}) as r:assert r.status==200
    assert len(g['calls'][1])==2 and len(g['calls'][0])==1


def test_cross_origin_requires_separate_explicit_credentials(tmp_path):
    store=Store(tmp_path)
    with pytest.raises(PolicyError,match='独立 key_env'):
        RoutingGuard(Config(upstream='https://anyrouter.top',guard={'models':['m']},routes=[{'name':'other','upstream':'https://other.example'}]),store)
    store.close()


def test_stateful_envelopes_are_not_classed_as_retryable_text():
    assert stateless_text(payload(),{})
    for p in (payload(previous_response_id='r'),payload(tools=[{}]),payload(background=True),payload(input=[{'type':'reasoning','encrypted_content':'opaque'}])):
        assert not stateless_text(p,{})
    assert not stateless_text(payload(),{'Cookie':'binding'})


def test_api_key_header_credentials_have_separate_health_buckets(tmp_path):
    store=Store(tmp_path)
    guard=RoutingGuard(Config(data_dir=str(tmp_path),guard={'models':['m']}),store)
    route=guard.routes[0]
    key_a=guard.key(route,{'x-api-key':'key-a'})
    key_b=guard.key(route,{'api-key':'key-b'})
    assert key_a=='key-a' and key_b=='key-b'
    assert guard.bucket(route,key_a,'m','high')!=guard.bucket(route,key_b,'m','high')
    store.close()


async def test_buffer_limit_rejects_without_replaying(gateway):
    g=gateway;g['service'].guard.limit=16
    async with g['client'].post(g['url'],json=payload()) as r:assert r.status==422
    assert len(g['calls'][0])==1 and not g['calls'][1]
    assert g['service'].store.list()[0]['routing']['delivery']=='rejected'


async def test_recent_failure_outweighs_large_historical_success_count(gateway):
    import time
    g=gateway;guard=g['service'].guard
    first=guard.routes[0];key=guard.key(first,{})
    bucket=guard.bucket(first,key,'m','high')
    guard.state['health'][bucket]={'route':'a','model':'m','target':'high','observations':10001,'accepted':10000,
        'recent':[True]*19+[False],'last_seen':time.time(),'cooldown_until':0}
    choice=guard.choose(payload(),{},'high',set())
    assert choice[0].name=='b'


async def test_new_tool_session_selects_preferred_route_before_send_but_never_replays(gateway):
    g=gateway;g['service'].guard.prefer('b');g['levels'][1]='low'
    p=payload(input=[{'role':'user','content':'a new task'}],tools=[{'type':'function','name':'write_file'}])
    async with g['client'].post(g['url'],json=p,headers={'session_id':'fresh-session'}) as r:assert r.status==422
    assert not g['calls'][0] and len(g['calls'][1])==1
