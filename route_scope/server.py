import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, WSMsgType, web
from multidict import CIMultiDict
from yarl import URL

from .evidence import Observation, parse_json
from .store import Store

HOP = {"host", "connection", "proxy-connection", "proxy-authorization", "proxy-authenticate", "keep-alive", "te", "trailer", "transfer-encoding", "upgrade", "content-length"}
STATIC = Path(__file__).parent / "static"


def forward_headers(headers, websocket=False):
    skip = HOP | {x.strip().lower() for x in headers.get("Connection", "").split(",")}
    if websocket:
        skip |= {"sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions", "sec-websocket-protocol"}
    return CIMultiDict((k, v) for k, v in headers.items() if k.lower() not in skip)


@dataclass
class Config:
    upstream: str = "https://anyrouter.top"
    proxy_host: str = "127.0.0.1"
    proxy_port: int = 15723
    dashboard_port: int = 15724
    data_dir: str = "data"
    outbound_proxy: str | None = None
    capture_bodies: bool = True
    body_limit: int = 2097152
    retention: int = 1000
    read_timeout: int = 600
    guard: dict | None = None
    routes: list = field(default_factory=list)

    def validate(self):
        url = urlsplit(self.upstream)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("upstream 必须是无凭据、查询参数的 http(s) 地址")
        if self.proxy_host not in ("127.0.0.1", "::1"):
            raise ValueError("默认仅允许本机回环监听；Windows 与 WSL 各自运行一份，或使用 WSL mirrored 网络")
        if not (1 <= self.proxy_port <= 65535 and 1 <= self.dashboard_port <= 65535) or self.proxy_port == self.dashboard_port:
            raise ValueError("代理和面板需要两个不同的有效端口")
        if url.hostname in ("127.0.0.1", "localhost", "::1") and url.port in (self.proxy_port, self.dashboard_port):
            raise ValueError("upstream 指向自身会形成代理环路")
        if self.body_limit < 1024 or self.retention < 1 or self.read_timeout < 1:
            raise ValueError("body_limit >= 1024, retention/read_timeout >= 1")


class Service:
    def __init__(self, config, viewer=False, store_override=None):
        config.validate()
        self.config = config
        self.viewer = viewer
        self.guard_lock=None
        if viewer and store_override is None and any((Path(config.data_dir)/name).exists() for name in ('storage.json','capture-index.json','watch-status.json')):
            from .catalog import Catalog
            store_override=Catalog(config.data_dir,config.retention)
        if config.guard is not None and not viewer:
            from .runtime import InstanceLock
            self.guard_lock=InstanceLock(Path(config.data_dir)/'guard.lock')
            self.guard_lock.__enter__()
        try:
            self.store = store_override or Store(config.data_dir, config.retention, recover=not viewer)
        except Exception:
            if self.guard_lock:self.guard_lock.__exit__(None,None,None)
            raise
        self.client = None
        self.runners = []
        self.active = set()
        self.guard = None
        if config.guard is not None and not viewer:
            from .routing import RoutingGuard
            try:self.guard = RoutingGuard(config,self.store)
            except Exception:
                self.store.close()
                if self.guard_lock:self.guard_lock.__exit__(None,None,None)
                raise

    def observation(self, request, body, source="reverse-proxy"):
        obs = Observation(request.method, request.path, request.headers, body, self.store.salt,
                          self.config.capture_bodies, self.config.body_limit, source)
        obs.record["upstream"] = self.config.upstream
        self.store.save(obs.refresh())
        return obs

    def target(self, raw_path):
        # Raw query escaping and repeated keys survive; the origin is fixed.
        return URL(self.config.upstream.rstrip("/") + raw_path, encoded=True)

    async def forward(self, request):
        if self.guard is not None:
            return await self.guard.forward(self,request)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.websocket(request)
        body = await request.read()
        obs = self.observation(request, body)
        response = None
        error = None
        last_save = 0
        task = asyncio.current_task()
        self.active.add(task)
        try:
            async with self.client.request(request.method, self.target(request.raw_path), data=body,
                                           headers=forward_headers(request.headers),
                                           proxy=self.config.outbound_proxy, allow_redirects=False) as upstream:
                obs.headers(upstream.status, upstream.headers)
                self.store.save(obs.refresh())
                response = web.StreamResponse(status=upstream.status, reason=upstream.reason,
                                              headers=forward_headers(upstream.headers))
                await response.prepare(request)
                async for chunk in upstream.content.iter_any():
                    # Original wire payload forwarded, compression included. No model/effort rewrite.
                    await response.write(chunk)
                    obs.feed(chunk)
                    if time.monotonic() - last_save > 0.5:
                        self.store.save(obs.refresh())
                        last_save = time.monotonic()
                await response.write_eof()
        except asyncio.CancelledError:
            error = "cancelled"
            raise
        except Exception as exc:
            error = type(exc).__name__  # Exception text can contain credential-bearing URLs.
            if response is None or not response.prepared:
                response = web.json_response({"error": {"message": "Route Scope upstream connection failed", "type": error}}, status=502)
            else:
                response.force_close()
                if request.transport:
                    request.transport.close()
        finally:
            try:
                self.store.save(obs.finish(error))
            except Exception:
                obs.record.update(state="interrupted", parse_errors=["final_parse_error"])
                self.store.save(obs.refresh())
            self.active.discard(task)
        return response

    async def websocket(self, request):
        protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
        observations = []
        pending = deque()
        by_response = {}
        upstream = None
        downstream = None
        error = None
        task = asyncio.current_task()
        self.active.add(task)
        try:
            upstream = await self.client.ws_connect(self.target(request.raw_path),
                headers=forward_headers(request.headers, websocket=True), protocols=protocols,
                proxy=self.config.outbound_proxy, autoping=False, autoclose=False, max_msg_size=32 * 1024 * 1024)
            downstream = web.WebSocketResponse(protocols=[upstream.protocol] if upstream.protocol else [],
                                               autoping=False, autoclose=False, max_msg_size=32 * 1024 * 1024)
            await downstream.prepare(request)

            async def pump(src, dst, from_client):
                nonlocal error
                async for msg in src:
                    if msg.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                        data = msg.data.encode() if isinstance(msg.data, str) else msg.data
                        payload = parse_json(data)
                        if from_client and payload.get("type") == "response.create":
                            obs = self.observation(request, data, "websocket")
                            obs.headers(101, {})
                            if pending:
                                obs.record["attribution_uncertain"] = True
                                for old in pending:
                                    old.record["attribution_uncertain"] = True
                            pending.append(obs)
                            observations.append(obs)
                        elif not from_client:
                            rid = (payload.get("response") or {}).get("id") if isinstance(payload.get("response"), dict) else payload.get("response_id")
                            obs = by_response.get(rid)
                            if obs is None and len(pending) == 1:
                                obs = pending[0]
                                if rid:
                                    by_response[rid] = obs
                            if obs is not None:
                                if "first_byte_ms" not in obs.record:
                                    obs.record["first_byte_ms"] = round((time.monotonic() - obs.started) * 1000)
                                obs.digest.update(data)
                                obs.total_bytes += len(data)
                                obs.record["decoded_bytes"] = obs.record.get("decoded_bytes", 0) + len(data) + 1
                                if obs.capture:
                                    obs.prefix.extend((data + b"\n")[:max(0, obs.limit - len(obs.prefix))])
                                try:
                                    obs.event(payload)
                                except Exception:
                                    obs.error("websocket_event_parse")
                                if obs.record["state"] not in ("sending", "streaming"):
                                    if obs in pending:
                                        pending.remove(obs)
                                    if rid:
                                        by_response.pop(rid, None)
                                    # WS frames are parsed individually, not a JSON response at EOF.
                                    obs.disabled = True
                                    self.store.save(obs.finish())
                                else:
                                    self.store.save(obs.refresh())
                        if msg.type == WSMsgType.TEXT:
                            await dst.send_str(msg.data)
                        else:
                            await dst.send_bytes(msg.data)
                    elif msg.type == WSMsgType.PING:
                        await dst.ping(msg.data)
                    elif msg.type == WSMsgType.PONG:
                        await dst.pong(msg.data)
                    elif msg.type == WSMsgType.CLOSE:
                        await dst.close(code=msg.data or 1000, message=str(msg.extra or "").encode())
                        break
                    elif msg.type == WSMsgType.ERROR:
                        error = "websocket_transport_error"
                        break

            tasks = [asyncio.create_task(pump(downstream, upstream, True)), asyncio.create_task(pump(upstream, downstream, False))]
            try:
                done, waiting = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for ended in done:
                    await ended
            finally:
                for child in tasks:
                    if not child.done():
                        child.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except asyncio.CancelledError:
            error = "cancelled"
            raise
        except Exception as exc:
            error = type(exc).__name__
            if not observations:
                obs = self.observation(request, b"", "websocket-handshake")
                obs.record["http_status"] = getattr(exc, "status", None)
                observations.append(obs)
            if downstream is None:
                return web.json_response({"error": {"type": error, "message": "WebSocket upstream handshake failed"}}, status=502)
        finally:
            for obs in observations:
                if obs.record["state"] in ("sending", "streaming"):
                    obs.disabled = True
                    self.store.save(obs.finish(error))
            if upstream:
                await upstream.close()
            if downstream:
                await downstream.close()
            self.active.discard(task)
        return downstream

    async def start(self):
        self.client = ClientSession(auto_decompress=False, cookie_jar=DummyCookieJar(), trust_env=False,
            timeout=ClientTimeout(total=None, connect=30, sock_read=self.config.read_timeout),
            skip_auto_headers={"Accept-Encoding", "User-Agent", "Content-Type"})
        proxy = web.Application(client_max_size=32 * 1024 * 1024)
        proxy.router.add_route("*", "/{path:.*}", self.forward)
        # Disable server-side automatic request decompression to preserve request bytes.
        proxy_runner = web.AppRunner(proxy, access_log=None, auto_decompress=False, shutdown_timeout=3,handler_cancellation=self.guard is not None)
        dashboard = web.Application(middlewares=[local_dashboard])
        async def static(request):
            name = {"/":"index.html", "/app.js":"app.js", "/style.css":"style.css"}[request.path]
            return web.FileResponse(STATIC / name)
        for path in ("/", "/app.js", "/style.css"):
            dashboard.router.add_get(path, static)
        dashboard.router.add_get("/api/status", self.status)
        dashboard.router.add_get("/api/captures", self.captures)
        dashboard.router.add_get("/api/captures/{id}", self.detail)
        dashboard.router.add_get("/api/export", self.export)
        if self.guard is not None:
            dashboard.router.add_get('/api/routing',self.routing_status)
            dashboard.router.add_post('/api/routing/preference',self.routing_preference)
        if hasattr(self.store,'runtime_status'):
            dashboard.router.add_post('/api/watch/control',self.watch_control)
        ui_runner = web.AppRunner(dashboard, access_log=None, shutdown_timeout=3)
        try:
            listeners = [(ui_runner, "127.0.0.1", self.config.dashboard_port)]
            if not self.viewer:
                listeners.insert(0, (proxy_runner, self.config.proxy_host, self.config.proxy_port))
            for runner, host, port in listeners:
                self.runners.append(runner)
                await runner.setup()
                await web.TCPSite(runner, host, port).start()
        except Exception:
            await self.close()
            raise

    async def status(self, request):
        return web.json_response({"upstream": self.config.upstream,
            "proxy": f"http://{self.config.proxy_host}:{self.config.proxy_port}",
            "viewer": self.viewer,
            "watch": self.store.runtime_status() if hasattr(self.store,'runtime_status') else None,
            "routing": self.guard.public_state() if self.guard is not None else None,
            "capture_bodies": self.config.capture_bodies, "retention": self.config.retention,
            "outbound_proxy_enabled": bool(self.config.outbound_proxy), "active": len(self.active)})

    async def routing_status(self,request):
        return web.json_response(self.guard.public_state())

    async def routing_preference(self,request):
        if request.content_type!='application/json':raise web.HTTPUnsupportedMediaType()
        try:
            value=await request.json()
            if not isinstance(value,dict) or 'route' not in value:raise ValueError()
            return web.json_response(self.guard.prefer(value['route']))
        except (ValueError,TypeError):raise web.HTTPBadRequest(text='Invalid route preference')

    async def watch_control(self, request):
        from .runtime import atomic_json, read_json
        if request.content_type!='application/json':raise web.HTTPUnsupportedMediaType()
        try:value=await request.json()
        except ValueError:raise web.HTTPBadRequest()
        action=value.get('action') if isinstance(value,dict) else None
        if action not in ('pause','resume'):raise web.HTTPBadRequest(text='Unsupported capture action')
        status=self.store.runtime_status()
        if not status or status.get('state')!='running' or status.get('stale'):
            raise web.HTTPConflict(text='Capture supervisor is not running')
        path=self.store.directory/'control.json'
        control=read_json(path)
        if control.get('stop_run_id')==status.get('run_id'):
            raise web.HTTPConflict(text='Capture supervisor is stopping')
        atomic_json(path,{'paused':action=='pause'})
        return web.json_response({'paused':action=='pause'})

    async def captures(self, request):
        items=self.store.list()
        return web.json_response({"items":items,"errors":list(getattr(self.store,'read_errors',{}).values()),
            'snapshot_epochs':getattr(self.store,'snapshots',{})})

    async def detail(self, request):
        record = self.store.get(request.match_info["id"])
        if record is None:
            if getattr(self.store,'read_errors',{}):raise web.HTTPServiceUnavailable(text='Capture source read failed')
            raise web.HTTPNotFound()
        return web.json_response(record)

    async def export(self, request):
        data = "\n".join(json.dumps(x, ensure_ascii=False) for x in self.store.list(full=True))
        if getattr(self.store,'read_errors',{}):raise web.HTTPServiceUnavailable(text='Capture export incomplete: source read failed')
        return web.Response(text=data, content_type="application/x-ndjson",
                            headers={"Content-Disposition": 'attachment; filename="route-scope-captures.jsonl"'})

    async def close(self):
        for runner in self.runners:
            await runner.cleanup()
        self.runners.clear()
        if self.client:
            await self.client.close()
            self.client = None
        self.store.close()
        if self.guard_lock:self.guard_lock.__exit__(None,None,None);self.guard_lock=None


@web.middleware
async def local_dashboard(request, handler):
    host = request.host.split(":")[0]
    if host not in ("127.0.0.1", "localhost"):
        raise web.HTTPForbidden(text="Local dashboard only")
    origin = request.headers.get("Origin")
    if origin and origin != f"{request.scheme}://{request.host}":
        raise web.HTTPForbidden(text="Cross-origin access denied")
    response = await handler(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'"
    return response
