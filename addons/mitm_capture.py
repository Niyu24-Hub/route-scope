"""Optional mitmdump adapter. Install route-scope into the mitmproxy environment first.

mitmdump --mode reverse:https://anyrouter.top@15923 -s addons/mitm_capture.py \
  --set route_scope_data=data/mitm --set route_scope_hosts=anyrouter.top

For encrypted forward interception, follow mitmproxy's per-client CA setup guide.
Never disable upstream certificate verification. Only explicitly listed hosts are recorded.
"""
import time

from mitmproxy import ctx

from route_scope.evidence import Observation
from route_scope.store import Store


class Capture:
    def __init__(self):
        self.store = None
        self.pending = {}

    def load(self, loader):
        loader.add_option("route_scope_data", str, "data/mitm", "Local capture directory")
        loader.add_option("route_scope_hosts", str, "anyrouter.top", "Comma-separated exact host allowlist")

    def running(self):
        self.store = Store(ctx.options.route_scope_data)

    def request(self, flow):
        if flow.request.host not in {s.strip() for s in ctx.options.route_scope_hosts.split(",")}:
            return
        if flow.request.headers.get("upgrade", "").lower() == "websocket":
            return  # WebSocket capture is provided by the primary reverse proxy.
        obs = Observation(flow.request.method, flow.request.path.split("?",1)[0], flow.request.headers,
                          flow.request.raw_content or b"", self.store.salt, source="mitmproxy")
        self.pending[flow.id] = obs
        self.store.save(obs.refresh())

    def responseheaders(self, flow):
        obs = self.pending.get(flow.id)
        if not obs:
            return
        obs.headers(flow.response.status_code, flow.response.headers)
        self.store.save(obs.refresh())
        last_save = [0.0]

        def stream(data):
            obs.feed(data)
            if time.monotonic() - last_save[0] > 0.5:
                self.store.save(obs.refresh())
                last_save[0] = time.monotonic()
            return data
        flow.response.stream = stream

    def response(self, flow):
        obs = self.pending.pop(flow.id, None)
        if obs:
            self.store.save(obs.finish())

    def error(self, flow):
        obs = self.pending.pop(flow.id, None)
        if obs:
            self.store.save(obs.finish("mitmproxy_flow_error"))

    def done(self):
        if self.store:
            for obs in self.pending.values():
                self.store.save(obs.finish("mitmproxy_stopped"))
            self.store.close()


addons = [Capture()]
