import json
import pytest

from route_scope.passive import Conversation, HTTPStream, TCPStream
from route_scope.store import Store


def test_tcp_retransmission_and_out_of_order_deduplication():
    chunks=[]; tcp=TCPStream(chunks.append,100)
    tcp.feed(103,b'def'); tcp.feed(100,b'abc'); tcp.feed(100,b'abcdef')
    tcp.feed(105,b'fgh')
    assert b''.join(chunks)==b'abcdefgh'


def test_http_fragmented_headers_chunk_extensions_and_trailers():
    events=[];body=[]
    p=HTTPStream(False,lambda status,headers:events.append(status),body.append,lambda:events.append('done'))
    raw=b'HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3;foo=bar\r\nabc\r\n2\r\nde\r\n0\r\nx-test: ok\r\n\r\n'
    for b in raw: p.feed(bytes([b]))
    assert events==[200,'done'] and b''.join(body)==b'abcde'


def test_mid_response_attachment_does_not_invent_request():
    events=[]
    p=HTTPStream(False,lambda *args:events.append('start'),lambda b:None,lambda:events.append('end'))
    p.feed(b'data: {"type":"response.completed"}\n\n')
    assert not events and p.ignored_prefix


@pytest.mark.parametrize('payload,reason',[(b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n','passive_http2_not_supported'),(b'\x16\x03\x03\x00\x12','passive_tls_not_supported')])
def test_unsupported_wire_protocol_is_explicit(payload,reason):
    p=HTTPStream(True,lambda *a:None,lambda b:None,lambda:None)
    with pytest.raises(ValueError,match=reason):p.feed(payload)


def test_passive_request_response_pair_and_boundary(tmp_path):
    store=Store(tmp_path)
    counts={'requests':0,'responses':0,'responses_without_request':0}
    c=Conversation(store,49111,counts)
    body=b'{"model":"actual-alias","reasoning":{"effort":"medium"}}'
    request=b'POST /v1/responses HTTP/1.1\r\nAuthorization: Bearer private-key\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body
    result={'type':'response.completed','response':{'id':'actual-response','model':'actual-alias','reasoning':{'effort':'low'},'status':'completed'}}
    data=('data: '+json.dumps(result)+'\n\n').encode()
    response=b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nTransfer-Encoding: chunked\r\n\r\n'+hex(len(data))[2:].encode()+b'\r\n'+data+b'\r\n0\r\n\r\n'
    for i in range(0,len(request),13): c.tcp[0].feed(100+i,request[i:i+13])
    for i in range(0,len(response),17): c.tcp[1].feed(900+i,response[i:i+17])
    r=store.list(full=True)[0]
    assert r['capture_boundary']=='client_to_ccswitch'
    assert r['upstream_request_observed'] is False
    assert r['requested']['value']=='medium' and r['final']['value']=='low'
    assert r['verdict']=='lower' and 'CC Switch' in r['verdict_label']
    assert 'private-key' not in json.dumps(r)
    assert counts['requests']==counts['responses']==1
    store.close()


def test_capture_end_is_not_a_model_failure_and_terminal_survives_reset(tmp_path):
    store=Store(tmp_path)
    counts={'requests':0,'responses':0,'responses_without_request':0}
    c=Conversation(store,49112,counts)
    c.request_start(('POST','/v1/responses'),{})
    c.request_data(b'{"model":"m","reasoning":{"effort":"xhigh"}}');c.request_end()
    c.response_start(200,{'content-type':'text/event-stream'})
    c.response_data(b'data: {"type":"response.completed","response":{"model":"m","status":"completed","reasoning":{"effort":"low"}}}\n\n')
    c.close('connection_reset')
    r=store.list()[0]
    assert r['state']=='completed' and r['verdict']=='lower'
    assert r['capture_end_reason']=='connection_reset' and r['http_message_complete'] is False
    assert r['terminal_event_observed'] is True
    c=Conversation(store,49113,counts)
    c.request_start(('POST','/v1/responses'),{});c.request_end();c.close('capture_window_ended')
    assert store.list()[0]['state']=='interrupted'
    store.close()
