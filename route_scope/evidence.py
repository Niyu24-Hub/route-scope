"""Protocol observations, never an inference of private upstream compute."""
import codecs
import hashlib
import hmac
import json
import re
import time
import uuid
import zlib
from urllib.parse import urlsplit
from datetime import datetime, timezone

# Comparison order only: never a model capability list or a capture whitelist.
# All raw values are retained; unranked values can differ but cannot be "lower".
KNOWN_EFFORT_ORDER = {name: i for i, name in enumerate(("none", "minimal", "low", "medium", "high", "xhigh", "max"))}
TERMINALS = {"response.completed", "response.incomplete", "response.failed"}
SECRET_KEYS = re.compile(r"authorization|cookie|api.?key|access.?token|refresh.?token|password|secret|encrypted_content", re.I)
TOKEN_TEXT = re.compile(r"(?i)\bBearer\s+[^\s\"'<>]+|\bsk-[A-Za-z0-9_-]{8,}")
IDENTITY_HEADERS = {"x-account-id", "chatgpt-account-id", "x-upstream-account-id", "x-channel-id", "x-provider-id"}
SESSION_HEADERS = {"session-id", "session_id", "thread-id", "x-session-id"}


def obj(value):
    return value if isinstance(value, dict) else {}


def parse_json(data):
    try:
        return obj(json.loads(data))
    except (ValueError, UnicodeError, TypeError):
        return {}


def decode_request(data, encoding):
    if encoding in ("gzip", "x-gzip"):
        return zlib.decompress(data, 16 + zlib.MAX_WBITS)
    if encoding == "deflate":
        return zlib.decompress(data)
    if encoding == "br":
        import brotli
        return brotli.decompress(data)
    if encoding == "zstd":
        import zstandard
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=32 * 1024 * 1024)
    if encoding not in ("", "identity"):
        raise ValueError("unknown_request_encoding")
    return data


def fingerprint(value, salt):
    return hmac.new(salt, str(value).encode(), hashlib.sha256).hexdigest()[:16]


def sanitize(value, secrets=()):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if SECRET_KEYS.search(k) else sanitize(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(x, secrets) for x in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret and len(secret) >= 6:
                value = value.replace(secret, "[REDACTED]")
        return TOKEN_TEXT.sub("[REDACTED]", value)
    return value


def safe_headers(headers, salt, secrets=()):
    result = {}
    for k, v in headers.items():
        key = k.lower()
        if SECRET_KEYS.search(key):
            result[key] = "[REDACTED]"
        elif key in IDENTITY_HEADERS or key in SESSION_HEADERS:
            result[key] = "hash:" + fingerprint(v, salt)
        else:
            result[key] = sanitize(v, secrets)
    return result


def effort(payload):
    nested = obj(payload.get("reasoning"))
    if "effort" in nested:
        return {"present": True, "value": nested["effort"], "path": "reasoning.effort"}
    if "reasoning_effort" in payload:
        return {"present": True, "value": payload["reasoning_effort"], "path": "reasoning_effort"}
    output_config = obj(payload.get("output_config"))
    if "effort" in output_config:
        return {"present": True, "value": output_config["effort"], "path": "output_config.effort"}
    return {"present": False, "value": None, "path": None}


def message_request_fields(payload):
    """Capture declarations without inferring an effective per-turn setting."""
    thinking = obj(payload.get('thinking'))
    overrides = []
    messages = payload.get('messages')
    for index, message in enumerate(messages if isinstance(messages, list) else []):
        config = obj(obj(message).get('output_config'))
        if 'effort' in config:
            overrides.append({'index': index, 'role': obj(message).get('role'),
                              'effort': {'present': True, 'value': config['effort'],
                                         'path': f'messages[{index}].output_config.effort'}})
    return {'request_thinking': {key: thinking[key] for key in ('type', 'budget_tokens', 'display') if key in thinking},
            'request_max_tokens': payload.get('max_tokens'), 'message_efforts': overrides}


def token_fields(usage, protocol=None):
    usage = obj(usage)
    output = obj(usage.get('output_tokens_details'))
    completion = obj(usage.get('completion_tokens_details'))
    candidates = [('usage.output_tokens_details.reasoning_tokens', output, 'reasoning_tokens'),
                  ('usage.completion_tokens_details.reasoning_tokens', completion, 'reasoning_tokens')]
    thinking = ('usage.output_tokens_details.thinking_tokens', output, 'thinking_tokens')
    candidates = [thinking, *candidates] if protocol == 'anthropic_messages' else [*candidates, thinking]
    source, value = None, None
    for path, details, key in candidates:
        if key in details:
            source, value = path, details[key]
            break
    valid = lambda value: value if type(value) is int and value >= 0 else None
    return {'reasoning_tokens': valid(value), 'reasoning_tokens_source': source,
            'thinking_tokens': valid(output.get('thinking_tokens'))}


def enrich_record(record):
    """Read compatibility for existing databases/snapshots; never rewrite evidence."""
    result = dict(record)
    if (result.get('protocol') == 'anthropic_messages' or result.get('first_event') == 'message_start'
            or urlsplit(result.get('path') or '').path.rstrip('/').endswith('/messages')):
        result['protocol'] = 'anthropic_messages'
        if isinstance(result.get('request_body'), dict):
            for key, value in message_request_fields(result['request_body']).items():
                result.setdefault(key, value)
        result.update(token_fields(result.get('usage'), result['protocol']))
        result['verdict'], result['verdict_label'] = verdict(result)
    return result


def verdict(record):
    if record.get("transport_error") or record.get("state") == "failed" or (record.get("http_status") or 0) >= 400:
        return "failed", "请求失败"
    if record.get("state") in ("sending", "streaming"):
        return "pending", "等待完成"
    if record.get("state") != "completed":
        return "incomplete", "返回未完整完成"
    if record.get("parse_errors") or record.get("attribution_uncertain"):
        return "unknown", "报文解析或归属不完整"
    if record.get('message_efforts'):
        return 'unknown', '存在消息级强度 · 需按消息核对'
    if (record.get('protocol') == 'anthropic_messages'
            and not obj(record.get('first')).get('present')
            and not obj(record.get('final')).get('present')):
        return 'no_echo', '已完成 · 标准协议无强度回显'
    requested = obj(record.get("requested")).get("value")
    returned = obj(record.get("final")).get("value")
    if not isinstance(requested, str) or not requested.strip() or not isinstance(returned, str) or not returned.strip():
        return "unknown", "强度字段缺失或为空"
    if requested == returned:
        if record.get("requested_model") and record.get("returned_model") and record["requested_model"] != record["returned_model"]:
            return "changed", "强度一致 · 模型名称不同"
        first = obj(record.get("first")).get("value")
        if isinstance(first, str) and first != requested:
            return "changed", "中间回显不同 · 最终一致"
        return "match", "回显一致 · 算力未验证"
    if requested in KNOWN_EFFORT_ORDER and returned in KNOWN_EFFORT_ORDER and KNOWN_EFFORT_ORDER[returned] < KNOWN_EFFORT_ORDER[requested]:
        return "lower", "上游回显较低"
    return "changed", "上游回显不同"


class SSEParser:
    """Incremental UTF-8, CR/LF, multiline data and comment handling."""
    def __init__(self, callback, max_event=8 * 1024 * 1024):
        self.callback = callback
        self.decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self.buffer = ""
        self.data = []
        self.kind = ""
        self.size = 0
        self.max_event = max_event

    def feed(self, chunk, final=False):
        self.buffer += self.decoder.decode(chunk, final=final)
        while True:
            match = re.search(r"\r\n|\r|\n", self.buffer)
            if not match or (match.group() == "\r" and match.end() == len(self.buffer) and not final):
                break
            line, self.buffer = self.buffer[:match.start()], self.buffer[match.end():]
            if not line:
                if self.data:
                    self.callback("\n".join(self.data), self.kind)
                self.data, self.kind, self.size = [], "", 0
            elif not line.startswith(":"):
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "data":
                    self.data.append(value)
                    self.size += len(value)
                elif field == "event":
                    self.kind = value
            if self.size > self.max_event:
                raise ValueError("sse_event_limit")
        if len(self.buffer) > self.max_event:
            raise ValueError("sse_line_limit")
        # No synthetic event at EOF: SSE requires a blank-line event delimiter.


class Observation:
    def __init__(self, method, path, headers, body, salt, capture=True, body_limit=2097152, source="reverse-proxy"):
        self.salt, self.capture, self.limit = salt, capture, body_limit
        lower = {k.lower(): v for k, v in headers.items()}
        self.secrets = [v for k, v in lower.items() if SECRET_KEYS.search(k)]
        self.secrets += [v.split(" ", 1)[1] for v in list(self.secrets) if " " in v]
        request_parse_error = None
        try:
            decoded_body = decode_request(body, lower.get("content-encoding", "").lower())
            if len(decoded_body) > 32 * 1024 * 1024:
                raise ValueError("request_decoded_limit")
        except Exception:
            decoded_body = b""
            request_parse_error = "request_decode_failed"
        payload = parse_json(decoded_body)
        sid = next((lower[k] for k in SESSION_HEADERS if k in lower), None)
        sid = sid or obj(payload.get("metadata")).get("session_id") or payload.get("prompt_cache_key")
        self.started = time.monotonic()
        self.prefix = bytearray()
        self.digest = hashlib.sha256()
        self.total_bytes = 0
        self.parser = None
        self.decoder = None
        self.encoding = ""
        self.disabled = False
        self.json_buffer = bytearray()
        self.output = ""
        self.record = {
            "id": uuid.uuid4().hex, "started_at": datetime.now(timezone.utc).isoformat(),
            "source": source, "method": method, "path": sanitize(path, self.secrets),
            "session": fingerprint(sid, salt) if sid else None,
            "key_fingerprint": fingerprint(lower.get("authorization", lower.get("x-api-key")), salt) if lower.get("authorization") or lower.get("x-api-key") else None,
            "client": sanitize(lower.get("user-agent", "unknown"), self.secrets),
            "requested_model": payload.get("model"), "requested": effort(payload),
            "effort_origin": "captured_request_body",
            "first": None, "final": None, "first_model": None, "returned_model": None,
            "reasoning_tokens": None, "usage": None, "http_status": None,
            "state": "sending", "events": [], "parse_errors": [], "request_headers": safe_headers(headers, salt, self.secrets),
            "request_sha256": hashlib.sha256(body).hexdigest(), "request_bytes": len(body),
            "request_body": self.body_view(decoded_body) if capture else None,
            "request_truncated": len(decoded_body) > body_limit, "account_hints": {},
            "internal_account": "unknown", "actual_compute": "unverified",
        }
        if urlsplit(path).path.rstrip('/').endswith('/messages'):
            self.record['protocol'] = 'anthropic_messages'
            self.record.update(sanitize(message_request_fields(payload), self.secrets))
        if request_parse_error:
            self.record["parse_errors"].append(request_parse_error)
        nested, flat = obj(payload.get("reasoning")).get("effort"), payload.get("reasoning_effort")
        if nested is not None and flat is not None and nested != flat:
            self.record["parse_errors"].append("conflicting_request_effort_fields")

    def body_view(self, data):
        text = data[:self.limit].decode("utf-8", errors="replace")
        try:
            return sanitize(json.loads(text), self.secrets)
        except ValueError:
            # Redact JSON keys in SSE/unparsed text as well as common token forms.
            text = re.sub(r'(?i)("(?:[^"\\]*(?:token|api_key|authorization|password|secret|encrypted_content)[^"\\]*)"\s*:\s*)"(?:[^"\\]|\\.)*"', r'\1"[REDACTED]"', text)
            return sanitize(text, self.secrets)

    def headers(self, status, headers):
        self.record.update(http_status=status, state="streaming", response_headers=safe_headers(headers, self.salt, self.secrets))
        lower = {k.lower(): v for k, v in headers.items()}
        self.record["account_hints"] = {k: fingerprint(v, self.salt) for k, v in lower.items() if k in IDENTITY_HEADERS}
        self.encoding = lower.get("content-encoding", "").lower()
        self.record["content_encoding"] = self.encoding or "identity"
        if "text/event-stream" in lower.get("content-type", "").lower():
            self.parser = SSEParser(self.event_text)
        try:
            if self.encoding in ("gzip", "x-gzip"):
                self.decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            elif self.encoding == "deflate":
                self.decoder = zlib.decompressobj()
            elif self.encoding == "br":
                import brotli
                self.decoder = brotli.Decompressor()
            elif self.encoding == "zstd":
                import zstandard
                self.decoder = zstandard.ZstdDecompressor().decompressobj()
            elif self.encoding not in ("", "identity"):
                raise ValueError("unsupported_encoding")
        except Exception as exc:
            self.error("decode_setup_" + type(exc).__name__)

    def error(self, name):
        if name not in self.record["parse_errors"]:
            self.record["parse_errors"].append(name)
        self.disabled = True

    def feed(self, data):
        if not data:
            return
        self.digest.update(data)
        self.total_bytes += len(data)
        if "first_byte_ms" not in self.record:
            self.record["first_byte_ms"] = round((time.monotonic() - self.started) * 1000)
        if self.disabled:
            return
        try:
            if self.decoder:
                decoded = self.decoder.process(data) if self.encoding == "br" else self.decoder.decompress(data)
            else:
                decoded = data
            if len(decoded) > 32 * 1024 * 1024:
                raise ValueError("decoded_chunk_limit")
            if self.capture:
                self.prefix.extend(decoded[:max(0, self.limit - len(self.prefix))])
            self.record["decoded_bytes"] = self.record.get("decoded_bytes", 0) + len(decoded)
            if self.parser:
                self.parser.feed(decoded)
            elif len(self.json_buffer) + len(decoded) <= 16 * 1024 * 1024:
                self.json_buffer.extend(decoded)
            else:
                raise ValueError("json_parse_limit")
        except Exception as exc:
            self.error("decode_or_parse_" + type(exc).__name__)

    def event_text(self, text, kind=""):
        if text.strip() == "[DONE]":
            if self.record.get("chat_finish"):
                self.record["state"] = "incomplete" if self.record["chat_finish"] in ("length", "content_filter") else "completed"
                self.record["terminal_event"] = "[DONE]"
            return
        try:
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("not_object")
            self.event(payload, kind)
        except (ValueError, TypeError):
            self.record["parse_errors"].append("invalid_event_json")

    def event(self, payload, kind="", plain_json=False):
        kind = payload.get("type") or kind
        if kind == 'message' or kind in ('message_start', 'message_delta', 'message_stop'):
            self.record['protocol'] = 'anthropic_messages'
        if kind in ("message_start", "message_delta", "message_stop", "content_block_delta") and not plain_json:
            self.message_event(payload, kind)
            return
        response = obj(payload.get("response"))
        terminal = kind in TERMINALS or plain_json
        if plain_json:
            response = payload
        if response:
            seen = effort(response)
            if self.record["first"] is None:
                self.record["first"] = seen
                self.record["first_model"] = response.get("model")
                self.record["first_event"] = kind or "json"
            event = {"event": kind or "json", "effort": seen, "model": response.get("model"), "response_id": response.get("id")}
            if len(self.record["events"]) < 100 or terminal:
                self.record["events"].append(event)
            if terminal:
                self.record["final"] = seen  # Explicit missing overwrites a previous echo.
                self.record["returned_model"] = response.get("model")
                self.record["response_id"] = response.get("id")
                self.record["terminal_event"] = kind or "json"
                state = response.get("status") or ("failed" if response.get("error") else "completed")
                if kind == "response.failed":
                    state = "failed"
                elif kind == "response.incomplete":
                    state = "incomplete"
                self.record["state"] = state
                if kind == "message":
                    self.record["stop_reason"] = response.get("stop_reason")
                    if response.get("stop_reason") in ("max_tokens", "model_context_window_exceeded"):
                        self.record["state"] = "incomplete"
                    elif not response.get('stop_reason') and self.record['state'] != 'failed':
                        self.record['parse_errors'].append('incomplete_message_sequence')
                        self.record['state'] = 'interrupted'
                self.record["incomplete_details"] = sanitize(response.get("incomplete_details"), self.secrets)
                self.record["upstream_error"] = sanitize(response.get("error"), self.secrets)
                texts = [c.get("text", "") for item in response.get("output", []) if isinstance(item, dict) for c in item.get("content", []) if isinstance(c, dict) and c.get("type") == "output_text"]
                if texts:
                    self.output = "".join(texts)[:self.limit]
                if kind == "message":
                    self.output = "".join(c.get("text", "") for c in response.get("content", [])
                        if isinstance(c, dict) and c.get("type") == "text")[:self.limit]
            self.usage(response.get("usage"))
        if kind == "response.output_text.delta":
            self.output = (self.output + str(payload.get("delta", "")))[:self.limit]
        if payload.get("error") or kind == "error":
            self.record["state"] = "failed"
            self.record["upstream_error"] = sanitize(payload.get("error", payload), self.secrets)
        if "choices" in payload:
            if self.record["first"] is None:
                self.record["first"] = effort(payload)
                self.record["first_model"] = payload.get("model")
            self.record["returned_model"] = payload.get("model", self.record["returned_model"])
            # Chat normally does not echo effort. Keep absent as absent.
            self.record["final"] = effort(payload)
            for choice in payload.get("choices", []):
                choice = obj(choice)
                content = obj(choice.get("delta", choice.get("message"))).get("content")
                if isinstance(content, str):
                    self.output = (self.output + content)[:self.limit]
                if choice.get("finish_reason"):
                    self.record["chat_finish"] = choice["finish_reason"]
                    if plain_json and choice["finish_reason"] in ("length", "content_filter"):
                        self.record["state"] = "incomplete"
            self.usage(payload.get("usage"))

    def message_event(self, payload, kind):
        record = self.record
        if kind == "message_start":
            message = obj(payload.get("message"))
            record.update(first=effort(message), first_model=message.get("model"), first_event=kind,
                          returned_model=message.get("model"), response_id=message.get("id"))
            self.usage(message.get("usage"))
        elif kind == "message_delta":
            delta = obj(payload.get("delta"))
            if "stop_reason" in delta:
                record["stop_reason"] = delta["stop_reason"]
            seen = effort(delta)
            if not seen['present']:
                seen = effort(payload)
            if seen['present']:
                record['message_delta_effort'] = seen
            self.usage(payload.get('usage'))
        elif kind == "content_block_delta":
            delta = obj(payload.get("delta"))
            if delta.get("type") == "text_delta":
                self.output = (self.output + str(delta.get("text", "")))[:self.limit]
        elif kind == "message_stop":
            record["terminal_event"] = kind
            record["final"] = effort(payload)
            if record['final']['present']:
                record['final_effort_event'] = kind
            elif record.get('message_delta_effort'):
                record['final'] = record['message_delta_effort']
                record['final_effort_event'] = 'message_delta'
            if record["state"] != "failed":
                if record.get("first_event") != "message_start" or not record.get("stop_reason"):
                    record["parse_errors"].append("incomplete_message_sequence")
                    record["state"] = "interrupted"
                else:
                    record["state"] = "incomplete" if record["stop_reason"] in (
                        "max_tokens", "model_context_window_exceeded") else "completed"
        if kind != "content_block_delta" and len(record["events"]) < 100:
            record["events"].append({"event": kind})

    def usage(self, usage):
        if isinstance(usage, dict):
            if self.record.get('protocol') == 'anthropic_messages':
                merged = dict(obj(self.record.get('usage')))
                for key, value in usage.items():
                    merged[key] = {**obj(merged.get(key)), **value} if isinstance(value, dict) else value
                usage = merged  # Message deltas report cumulative counts, not increments.
            self.record["usage"] = sanitize(usage, self.secrets)
            self.record.update(token_fields(usage, self.record.get('protocol')))

    def finish(self, transport_error=None):
        if transport_error:
            self.record.update(transport_error=transport_error, state="failed")
        if not self.disabled and not transport_error:
            if self.decoder:
                complete = self.decoder.is_finished() if self.encoding == "br" else getattr(self.decoder, "eof", True)
                if not complete:
                    self.error("compressed_stream_incomplete")
            if self.parser:
                try:
                    self.parser.feed(b"", final=True)
                except Exception:
                    self.error("invalid_sse_eof")
            elif self.json_buffer:
                value = parse_json(self.json_buffer)
                if value:
                    try:
                        self.event(value, plain_json=True)
                    except (TypeError, ValueError, AttributeError):
                        self.error("invalid_response_shape")
                else:
                    self.error("non_json_response")
        if self.record["state"] in ("sending", "streaming"):
            self.record["state"] = "interrupted"
        self.record.update(duration_ms=round((time.monotonic() - self.started) * 1000),
                           response_sha256=self.digest.hexdigest(), response_wire_bytes=self.total_bytes,
                           response_truncated=self.record.get("decoded_bytes", 0) > self.limit,
                           response_body=self.body_view(bytes(self.prefix)) if self.capture else None,
                           output_text=sanitize(self.output, self.secrets) if self.capture else None)
        self.refresh()
        return self.record

    def refresh(self):
        self.record["verdict"], self.record["verdict_label"] = verdict(self.record)
        self.record["model_changed"] = bool(self.record["returned_model"] and self.record["requested_model"] != self.record["returned_model"])
        return self.record
