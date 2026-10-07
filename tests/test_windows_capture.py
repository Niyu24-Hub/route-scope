import ctypes
import json
import os
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from route_scope.discovery import WindowsDiscovery
from route_scope.evidence import Observation
from route_scope.packet_sources import CaptureUnavailable, WindowsPackets
from route_scope.autocapture import CaptureEngine
from route_scope.store import Store


def test_discovery_keeps_draining_while_powershell_runs(monkeypatch):
    process = SimpleNamespace(returncode=None, poll=lambda: None)
    monkeypatch.setattr('route_scope.discovery.subprocess.Popen', lambda *a, **kw: process)
    discovery = WindowsDiscovery(b'salt')
    discovery.ports = {15721}
    discovery.scan()
    assert discovery.pending is process
    discovery.scan()
    assert discovery.ports == {15721}
    process.poll = lambda: 0
    process.communicate = lambda: (b'{"ports":[15722],"processes":[]}', b'')
    discovery.scan()
    assert discovery.ports == {15722}
    assert discovery.pending is None


def test_windows_filter_changes_only_when_ports_change():
    calls = []
    source = WindowsPackets.__new__(WindowsPackets)
    source.handle = 1
    source.ports = None
    source.BPFProgram = ctypes.c_int
    source.pcap = SimpleNamespace(
        pcap_compile=lambda *args: calls.append(args[2]) or 0,
        pcap_setfilter=lambda *args: 0,
        pcap_freecode=lambda *args: calls.append('free'))
    source.set_ports({15721})
    source.set_ports({15721})
    source.set_ports({15721, 15722})
    assert calls == [b'ip and tcp and host 127.0.0.1 and (port 15721)', 'free',
                     b'ip and tcp and host 127.0.0.1 and (port 15721 or port 15722)', 'free']
    source.pcap.pcap_setfilter = lambda *args: -1
    with pytest.raises(CaptureUnavailable):
        source.set_ports({12345})
    assert source.ports == (15721, 15722)
    assert calls[-1] == 'free'


@pytest.mark.parametrize('stop,state', [('end_turn', 'completed'), ('tool_use', 'completed'),
                                       ('max_tokens', 'incomplete')])
def test_claude_sse_completion_and_cumulative_usage(stop, state):
    obs = Observation('POST', '/v1/messages', {},
        b'{"model":"claude-fixture","output_config":{"effort":"high"}}', b'salt')
    obs.headers(200, {'content-type': 'text/event-stream'})
    events = [
        {'type': 'message_start', 'message': {'id': 'msg_fixture', 'model': 'claude-fixture',
            'usage': {'input_tokens': 20, 'output_tokens': 1}}},
        {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'hello'}},
        {'type': 'message_delta', 'delta': {'stop_reason': stop}, 'usage': {'output_tokens': 9}},
        {'type': 'message_stop'}]
    stream = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
    for offset in range(0, len(stream), 7):
        obs.feed(stream[offset:offset + 7])
    result = obs.finish()
    assert result['state'] == state
    assert result['requested']['path'] == 'output_config.effort'
    assert result['requested']['value'] == 'high'
    assert result['final']['present'] is False
    assert result['returned_model'] == 'claude-fixture'
    assert result['usage'] == {'input_tokens': 20, 'output_tokens': 9}
    assert result['output_text'] == 'hello'
    assert result['reasoning_tokens'] is None


def test_claude_missing_stop_event_does_not_claim_completion():
    obs = Observation('POST', '/v1/messages', {}, b'{}', b'salt')
    obs.headers(200, {'content-type': 'text/event-stream'})
    obs.event({'type': 'message_start', 'message': {'model': 'fixture'}})
    obs.event({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}})
    assert obs.finish()['state'] == 'interrupted'


def test_claude_error_is_not_overwritten_by_stop():
    obs = Observation('POST', '/v1/messages', {}, b'{}', b'salt')
    obs.headers(200, {'content-type': 'text/event-stream'})
    obs.event({'type': 'error', 'error': {'type': 'overloaded_error'}})
    obs.event({'type': 'message_stop'})
    assert obs.finish()['state'] == 'failed'


@pytest.mark.skipif(os.environ.get('ROUTE_SCOPE_TEST_NPCAP') != '1', reason='opt-in live Npcap check')
def test_live_npcap_large_claude_request(tmp_path):
    server = socket.socket()
    server.bind(('127.0.0.1', 0));server.listen(1)
    port = server.getsockname()[1]
    source = WindowsPackets()
    source.set_ports({port})
    store = Store(tmp_path)
    engine = CaptureEngine(store, SimpleNamespace(owners={}, attribute=lambda r: None), 'fixture')
    errors = []
    body = json.dumps({'model': 'local-fixture', 'output_config': {'effort': 'high'},
                       'messages': [{'role': 'user', 'content': 'x' * 512000}]}).encode()
    events = [{'type': 'message_start', 'message': {'model': 'local-fixture', 'id': 'fixture'}},
              {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 1}},
              {'type': 'message_stop'}]
    response = ''.join('data: ' + json.dumps(event) + '\n\n' for event in events).encode()
    def serve():
        try:
            server.settimeout(10)
            with server.accept()[0] as connection:
                connection.settimeout(10)
                data = b''
                while b'\r\n\r\n' not in data:
                    data += connection.recv(65536)
                _, received = data.split(b'\r\n\r\n', 1)
                while len(received) < len(body):
                    chunk = connection.recv(65536)
                    if not chunk:raise RuntimeError('fixture request closed early')
                    received += chunk
                assert received == body
                connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nContent-Length: '
                                   + str(len(response)).encode() + b'\r\nConnection: close\r\n\r\n' + response)
        except Exception as exc:errors.append(exc)
    def client():
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=10) as connection:
                connection.sendall(b'POST /v1/messages HTTP/1.1\r\nHost: localhost\r\nContent-Length: '
                                   + str(len(body)).encode() + b'\r\n\r\n' + body)
                while connection.recv(65536):pass
        except Exception as exc:errors.append(exc)
    threads = [threading.Thread(target=serve), threading.Thread(target=client)]
    try:
        for thread in threads:thread.start()
        deadline = time.monotonic() + 12
        while engine.stats['responses'] < 1 and time.monotonic() < deadline:
            received = source.receive()
            if received:engine.process(*received, {port})
            else:time.sleep(.005)
        for thread in threads:thread.join(timeout=12)
        assert not errors
        assert engine.stats['requests'] == engine.stats['responses'] == 1
        assert source.dropped() == 0
        record = store.list(full=True)[0]
        assert record['state'] == 'completed'
        assert record['request_bytes'] == len(body)
        assert record['http_message_complete'] is True
        assert record['terminal_event'] == 'message_stop'
    finally:
        source.close();engine.close('fixture_end');store.close();server.close()
