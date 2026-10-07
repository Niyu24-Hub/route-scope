import asyncio
import json

from aiohttp import ClientSession, web

from .cli import serve
from .server import Config

# Local protocol examples, not a capability list for any real model.
DEMO_SCENARIOS = (("none", "match"), ("minimal", "match"), ("low", "match"),
                  ("medium", "match"), ("high", "match"), ("xhigh", "match"),
                  ("max", "match"), ("vendor-custom", "match"), ("max", "lower"),
                  ("medium", "missing"), ("low", "failed"), ("high", "interrupted"),
                  ("high", "model"), (None, "match"))

def sse(kind, response):
    return ("event: " + kind + "\ndata: " + json.dumps({"type": kind, "response": response}, ensure_ascii=False) + "\n\n").encode()


async def mock_response(request):
    payload = await request.json()
    level = payload.get("reasoning", {}).get("effort", "high")
    scenario = request.headers.get("x-demo-scenario", "match")
    if scenario == "failed":
        return web.json_response({"error": {"message": "DEMO: unsupported effort for this upstream model"}}, status=400)
    response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "x-oneapi-request-id": "DEMO-request-id", "x-upstream-account-id": "DEMO-pool-account-A"})
    await response.prepare(request)
    echo = "high" if scenario == "lower" else level
    first = {"id": "resp_demo", "model": payload.get("model"), "status": "in_progress", "reasoning": {"effort": echo}}
    await response.write(sse("response.created", first))
    await asyncio.sleep(0.04)
    final = dict(first, status="completed", reasoning={"effort": "low" if scenario == "lower" else echo},
                 usage={"input_tokens": 200, "output_tokens": 450, "output_tokens_details": {"reasoning_tokens": 404}},
                 output=[{"type": "message", "content": [{"type": "output_text", "text": "这是本地协议演示报文，不是真实 anyrouter 返回。"}]}])
    if scenario == "missing":
        final.pop("reasoning")
    if scenario == "model":
        final["model"] = "demo-alternate-model"
    if scenario != "interrupted":
        await response.write(sse("response.completed", final))
    await response.write_eof()
    return response


async def seed_demo(port):
    async with ClientSession() as client:
        for level, scenario in DEMO_SCENARIOS:
            payload = {"model": "demo-model", "input": "DEMO ONLY", "stream": True}
            if level is not None:
                payload["reasoning"] = {"effort": level}
            async with client.post(f"http://127.0.0.1:{port}/v1/responses", json=payload,
                                   headers={"x-demo-scenario": scenario, "session_id": "demo-session", "Authorization": "Bearer DEMO-DO-NOT-USE", "User-Agent": "DEMO / Codex"}) as response:
                await response.read()


async def run_demo(args):
    app = web.Application()
    app.router.add_post("/v1/responses", mock_response)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", args.upstream_port).start()
    config = Config(upstream=f"http://127.0.0.1:{args.upstream_port}", proxy_port=args.proxy_port,
                    dashboard_port=args.dashboard_port, data_dir=args.data_dir)
    async def seed():
        await asyncio.sleep(0.4)
        await seed_demo(args.proxy_port)
        print(f"已生成 {len(DEMO_SCENARIOS)} 条本地协议演示记录；未请求 anyrouter，也未修改 CC Switch。", flush=True)
    seed_task = asyncio.create_task(seed())
    try:
        await serve(config)
    finally:
        if not seed_task.done():
            seed_task.cancel()
        await asyncio.gather(seed_task, return_exceptions=True)
        await runner.cleanup()
