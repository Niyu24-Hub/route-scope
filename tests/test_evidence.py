import gzip
import json

import pytest

from route_scope.evidence import Observation, SSEParser, sanitize


def observation(effort="max", **kwargs):
    return Observation("POST", "/v1/responses", {"Authorization": "Bearer secret-token-123", "session_id": "private-session"},
                       json.dumps({"model": "m", "reasoning": {"effort": effort}}).encode(), b"salt", **kwargs)


def frame(kind, response):
    return ("event: " + kind + "\r\ndata: " + json.dumps({"type": kind, "response": response}, ensure_ascii=False) + "\r\n\r\n").encode()


def response(level="max", **kwargs):
    return dict(id="r", model="m", status="completed", reasoning={"effort": level}, **kwargs)


def test_fragmented_sse_first_final_lower_and_zero_tokens():
    obs = observation()
    obs.headers(200, {"Content-Type": "text/event-stream"})
    data = frame("response.created", response("high")) + frame("response.completed", response("low", usage={"output_tokens_details": {"reasoning_tokens": 0}}))
    for b in data:
        obs.feed(bytes([b]))
    r = obs.finish()
    assert r["first"]["value"] == "high"
    assert r["final"]["value"] == "low"
    assert r["verdict"] == "lower" and r["reasoning_tokens"] == 0
    assert r["actual_compute"] == "unverified"


def test_missing_final_never_falls_back_to_created():
    obs = observation()
    obs.headers(200, {"Content-Type": "text/event-stream"})
    obs.feed(frame("response.created", response()))
    obs.feed(frame("response.completed", {"id": "r", "model": "m", "status": "completed"}))
    r = obs.finish()
    assert r["verdict"] == "unknown"
    assert r["final"]["present"] is False


@pytest.mark.parametrize("kind,state,expected", [("response.completed","completed","match"),("response.incomplete","incomplete","incomplete"),("response.failed","failed","failed")])
def test_terminal_states(kind, state, expected):
    obs = observation()
    obs.headers(200, {"Content-Type": "text/event-stream"})
    obs.feed(frame(kind, {**response(), "status": state}))
    assert obs.finish()["verdict"] == expected


def test_truncated_stream_not_success():
    obs = observation()
    obs.headers(200, {"Content-Type": "text/event-stream"})
    obs.feed(frame("response.created", response()))
    r = obs.finish()
    assert r["state"] == "interrupted" and r["final"] is None


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br", "zstd"])
def test_compression(encoding):
    import brotli, zstandard, zlib
    data = frame("response.completed", response())
    packed = {"gzip":gzip.compress, "deflate":zlib.compress, "br":brotli.compress, "zstd":zstandard.ZstdCompressor().compress}[encoding](data)
    obs = observation()
    obs.headers(200, {"Content-Type": "text/event-stream", "Content-Encoding": encoding})
    for start in range(0, len(packed), 7):
        obs.feed(packed[start:start+7])
    assert obs.finish()["verdict"] == "match"


def test_http_error_json_and_null_effort():
    obs = observation()
    obs.headers(400, {"Content-Type": "application/json"})
    obs.feed(b'{"error":{"message":"unsupported max"}}')
    assert obs.finish()["verdict"] == "failed"
    obs = observation()
    obs.headers(200, {})
    obs.feed(json.dumps(response(None)).encode())
    assert obs.finish()["verdict"] == "unknown"


def test_capture_cap_does_not_prevent_final_parsing():
    obs = observation(body_limit=30)
    obs.headers(200, {"Content-Type": "text/event-stream"})
    obs.feed(b": heartbeat\n\n" * 100)
    obs.feed(frame("response.completed", response()))
    r = obs.finish()
    assert r["response_truncated"] and r["verdict"] == "match"
    assert len(r["response_body"]) <= 30


def test_sse_comments_multiline_unicode_and_cr():
    results = []
    parser = SSEParser(lambda data, kind: results.append((json.loads(data), kind)))
    data = ': heartbeat\revent: hello\rdata: {"value":\rdata: "中文"}\r\r'.encode()
    for i in range(0,len(data),2):
        parser.feed(data[i:i+2])
    parser.feed(b"", final=True)
    assert results == [({"value":"中文"},"hello")]


def test_secret_redaction_and_session_hash():
    obs = observation()
    obs.headers(200, {"Set-Cookie":"private-cookie", "x-upstream-account-id":"real-account"})
    obs.feed(json.dumps(response(output=[{"content":[{"type":"output_text","text":"Bearer secret-token-123"}]}])).encode())
    r = obs.finish()
    dumped = json.dumps(r)
    assert "secret-token-123" not in dumped and "private-cookie" not in dumped and "real-account" not in dumped
    assert r["session"] != "private-session"
    assert sanitize({"encrypted_content":"private","nested":{"api_key":"key"}}) == {"encrypted_content":"[REDACTED]","nested":{"api_key":"[REDACTED]"}}


def test_chat_tokens_and_no_fabricated_effort():
    obs = observation("high")
    obs.headers(200, {"Content-Type":"text/event-stream"})
    for payload in ({"model":"m","choices":[{"delta":{"content":"hello"},"finish_reason":None}]},
                    {"model":"m","choices":[{"delta":{},"finish_reason":"stop"}]},
                    {"model":"m","choices":[],"usage":{"completion_tokens_details":{"reasoning_tokens":25}}}):
        obs.feed(("data: "+json.dumps(payload)+"\n\n").encode())
    obs.feed(b"data: [DONE]\n\n")
    r=obs.finish()
    assert r["state"] == "completed" and r["verdict"] == "unknown" and r["reasoning_tokens"] == 25
    assert r["output_text"] == "hello"


def test_malformed_event_flags_unknown_even_with_terminal():
    obs = observation()
    obs.headers(200, {"Content-Type":"text/event-stream"})
    obs.feed(b"data: {bad}\n\n"+frame("response.completed",response()))
    assert obs.finish()["verdict"] == "unknown"


def test_metadata_only_and_request_zstd():
    import zstandard
    body=json.dumps({"model":"m","reasoning":{"effort":"xhigh"}}).encode()
    compressed=zstandard.ZstdCompressor().compress(body)
    obs=Observation("POST","/responses",{"Content-Encoding":"zstd"},compressed,b"salt",capture=False)
    assert obs.record["requested"]["value"] == "xhigh"
    obs.headers(200,{})
    obs.feed(json.dumps(response("xhigh")).encode())
    r=obs.finish()
    assert r["request_body"] is None and r["response_body"] is None and r["output_text"] is None


def test_intermediate_change_and_model_name_are_distinct_from_exact_match():
    obs=observation()
    obs.headers(200,{"Content-Type":"text/event-stream"})
    obs.feed(frame("response.created",response("high"))+frame("response.completed",response("max")))
    assert obs.finish()["verdict"]=="changed"
    obs=observation()
    obs.headers(200,{})
    obs.feed(json.dumps({**response(),"model":"alias-model"}).encode())
    assert obs.finish()["verdict"]=="changed"


def test_truncated_gzip_does_not_claim_complete_evidence():
    obs=observation()
    obs.headers(200,{"Content-Type":"text/event-stream","Content-Encoding":"gzip"})
    obs.feed(gzip.compress(frame("response.completed",response()))[:-5])
    r=obs.finish()
    assert r["verdict"]=="unknown" and 'compressed_stream_incomplete' in r["parse_errors"]


def test_malformed_response_shape_is_recorded_without_crashing():
    obs=observation()
    obs.headers(200,{})
    obs.feed(json.dumps(response(output=None)).encode())
    assert obs.finish()["verdict"]=="unknown"


@pytest.mark.parametrize("value", ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "vendor-custom"])
@pytest.mark.parametrize("wire", ["responses", "chat"])
def test_every_observed_effort_is_preserved_without_capability_whitelist(value, wire):
    payload={"model":"literal-model-alias"}
    if wire=='responses':
        payload['reasoning']={'effort':value}
    else:
        payload['reasoning_effort']=value
    obs=Observation('POST','/v1/responses',{},json.dumps(payload).encode(),b'salt')
    assert obs.record['requested']['value']==value
    assert obs.record['requested']['path']==('reasoning.effort' if wire=='responses' else 'reasoning_effort')
    assert obs.record['effort_origin']=='captured_request_body'
    obs.headers(200,{})
    obs.feed(json.dumps({**response(value),'model':'literal-model-alias'}).encode())
    assert obs.finish()['verdict']=='match'


def test_missing_request_effort_is_never_filled_from_response_or_tokens():
    obs=Observation('POST','/v1/responses',{},b'{"model":"m"}',b'salt')
    obs.headers(200,{})
    obs.feed(json.dumps(response('max',usage={'output_tokens_details':{'reasoning_tokens':100000}})).encode())
    r=obs.finish()
    assert r['requested']=={'present':False,'value':None,'path':None}
    assert r['final']['value']=='max' and r['verdict']=='unknown'


def test_unknown_effort_changes_are_not_ranked_and_empty_stays_unknown():
    obs=observation('vendor-custom')
    obs.headers(200,{})
    obs.feed(json.dumps(response('low')).encode())
    assert obs.finish()['verdict']=='changed'
    obs=observation('')
    obs.headers(200,{})
    obs.feed(json.dumps(response('')).encode())
    assert obs.finish()['verdict']=='unknown'
