import json

import pytest

from route_scope.evidence import Observation, enrich_record
from route_scope.store import Store
from route_scope.snapshots import SnapshotStore
from route_scope.catalog import Catalog


def observe(payload=None, capture=True):
    return Observation('POST', '/v1/messages?beta=true', {}, json.dumps(payload or {
        'model': 'client-alias', 'output_config': {'effort': 'high'},
        'thinking': {'type': 'adaptive', 'display': 'omitted'}, 'max_tokens': 64000,
    }).encode(), b'salt', capture=capture)


def stream(obs, usage=None, stop='end_turn', extra=()):
    obs.headers(200, {'Content-Type': 'text/event-stream'})
    events = [{'type': 'message_start', 'message': {'model': 'returned-alias',
        'usage': {'input_tokens': 20, 'output_tokens': 1}}}, *extra,
        {'type': 'message_delta', 'delta': {'stop_reason': stop}, 'usage': usage or {}},
        {'type': 'message_stop'}]
    raw = ''.join('event: '+event['type']+'\ndata: '+json.dumps(event)+'\n\n' for event in events).encode()
    for offset in range(0, len(raw), 3):
        obs.feed(raw[offset:offset+3])
    return obs.finish()


@pytest.mark.parametrize('count', [0, 16, 2927])
def test_thinking_tokens_no_echo_does_not_mean_failure_or_low(count):
    r = stream(observe(), {'output_tokens': count+50, 'output_tokens_details': {'thinking_tokens': count}})
    assert r['reasoning_tokens'] == r['thinking_tokens'] == count
    assert r['reasoning_tokens_source'] == 'usage.output_tokens_details.thinking_tokens'
    assert r['requested']['value'] == 'high'
    assert r['state'] == 'completed' and r['verdict'] == 'no_echo'
    assert r['first']['present'] is False and r['final']['present'] is False
    assert r['requested_model'] == 'client-alias' and r['returned_model'] == 'returned-alias'
    assert r['request_thinking'] == {'type': 'adaptive', 'display': 'omitted'}
    assert r['request_max_tokens'] == 64000


@pytest.mark.parametrize('value', [None, -1, '2927', True, 1.5])
def test_invalid_token_value_is_not_coerced(value):
    r = stream(observe(), {'output_tokens_details': {'thinking_tokens': value}})
    assert r['thinking_tokens'] is None and r['reasoning_tokens'] is None
    assert r['usage']['output_tokens_details']['thinking_tokens'] == value


def test_cumulative_nested_usage_is_merged_not_added():
    extra = [
        {'type': 'message_delta', 'delta': {}, 'usage': {'output_tokens': 100,
            'output_tokens_details': {'thinking_tokens': 80, 'vendor_field': 7}}},
        {'type': 'message_delta', 'delta': {}, 'usage': {'output_tokens_details': {'thinking_tokens': 0}}},
    ]
    r = stream(observe(), {'output_tokens': 120}, extra=extra)
    assert r['usage'] == {'input_tokens': 20, 'output_tokens': 120,
        'output_tokens_details': {'thinking_tokens': 0, 'vendor_field': 7}}
    assert r['reasoning_tokens'] == 0


def test_metadata_only_retains_settings_not_prompt():
    obs = observe({'model': 'm', 'thinking': {'type': 'enabled', 'budget_tokens': 4096},
                   'messages': [{'role': 'user', 'content': 'private prompt'}]}, capture=False)
    r = stream(obs)
    assert r['requested']['present'] is False
    assert r['request_thinking']['budget_tokens'] == 4096
    assert r['request_body'] is None and r['response_body'] is None and r['output_text'] is None
    assert 'private prompt' not in json.dumps(r)


def test_message_efforts_preserve_paths_null_roles_without_effective_guess():
    r = stream(observe({'output_config': {'effort': 'high'}, 'messages': [
        {'role': 'system', 'output_config': {'effort': 'low'}},
        {'role': 'system', 'output_config': {'effort': None}},
        {'role': 'user', 'output_config': {'effort': 'vendor-custom'}},
    ]}))
    assert r['requested']['value'] == 'high'
    assert [m['effort']['value'] for m in r['message_efforts']] == ['low', None, 'vendor-custom']
    assert r['message_efforts'][2]['effort']['path'] == 'messages[2].output_config.effort'
    assert r['state'] == 'completed' and r['verdict'] == 'unknown'


@pytest.mark.parametrize('stop', ['max_tokens', 'model_context_window_exceeded'])
def test_incomplete_not_classified_as_normal_no_echo(stop):
    r = stream(observe(), stop=stop)
    assert r['state'] == 'incomplete' and r['verdict'] == 'incomplete'


def test_invalid_json_and_errors_not_masked_by_no_echo():
    obs = observe()
    obs.record['parse_errors'].append('invalid_event_json')
    assert stream(obs)['verdict'] == 'unknown'
    r = stream(observe(), extra=[{'type': 'error', 'error': {'type': 'overloaded_error'}}])
    assert r['verdict'] == 'failed'


def test_missing_stop_stays_interrupted():
    obs = observe()
    obs.headers(200, {'Content-Type': 'text/event-stream'})
    obs.event({'type': 'message_start', 'message': {'model': 'm'}})
    obs.event({'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}})
    assert obs.finish()['verdict'] == 'incomplete'


def test_vendor_delta_effort_is_preserved_and_compared():
    r = stream(observe(), extra=[{'type': 'message_delta', 'delta': {'output_config': {'effort': 'low'}}}])
    assert r['final']['value'] == 'low' and r['final_effort_event'] == 'message_delta'
    assert r['verdict'] == 'lower'


def test_plain_json_and_explicit_null_effort():
    obs = observe()
    obs.headers(200, {'Content-Type': 'application/json'})
    obs.feed(json.dumps({'type': 'message', 'model': 'm', 'stop_reason': 'end_turn', 'content': [],
        'usage': {'output_tokens_details': {'thinking_tokens': 0}}}).encode())
    assert obs.finish()['verdict'] == 'no_echo'
    obs = observe()
    r = stream(obs, extra=[{'type': 'message_delta', 'delta': {'output_config': {'effort': None}}}])
    assert r['final']['present'] is True and r['verdict'] == 'unknown'


def test_plain_json_missing_stop_reason_is_not_completed():
    obs = observe()
    obs.headers(200, {'Content-Type': 'application/json'})
    obs.feed(b'{"type":"message","model":"m","content":[]}')
    assert obs.finish()['verdict'] == 'incomplete'


@pytest.mark.parametrize('snapshot', [False, True])
def test_old_records_are_enriched_without_rewriting_db(tmp_path, snapshot):
    r = stream(observe(), {'output_tokens_details': {'thinking_tokens': 713}})
    for key in ('protocol', 'thinking_tokens', 'reasoning_tokens_source', 'request_thinking', 'message_efforts'):
        r.pop(key, None)
    r.update(reasoning_tokens=None, verdict='unknown', verdict_label='强度字段缺失或为空')
    store = SnapshotStore(tmp_path) if snapshot else Store(tmp_path)
    try:
        store.save(r)
        before = store.db.execute('select data from captures').fetchone()[0]
        if snapshot:
            store.publish_snapshot(force=True)
            reader = Catalog(tmp_path)
        else:
            reader = store
        for record in (reader.list()[0], reader.list(full=True)[0], reader.get(r['id'])):
            assert record['reasoning_tokens'] == 713 and record['verdict'] == 'no_echo'
        assert store.db.execute('select data from captures').fetchone()[0] == before
    finally:
        store.close()


def test_reasoning_tokens_precedence_is_protocol_specific():
    r = stream(observe(), {'output_tokens_details': {'thinking_tokens': 0, 'reasoning_tokens': 100}})
    assert r['reasoning_tokens'] == 0
    obs = Observation('POST', '/v1/responses', {}, b'{}', b'salt')
    obs.usage({'output_tokens_details': {'reasoning_tokens': 10, 'thinking_tokens': 99}})
    assert obs.record['reasoning_tokens'] == 10
