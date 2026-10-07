"""Run with python -I to verify the installed wheel, independent of checkout imports."""
import asyncio
import json
from pathlib import Path
import socket
import tempfile

from aiohttp import ClientSession, web
from route_scope import __version__
from route_scope.demo import DEMO_SCENARIOS, mock_response, seed_demo
from route_scope.server import Config, Service


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def main():
    with tempfile.TemporaryDirectory(prefix='route-scope-smoke-') as directory:
        upstream = web.Application()
        upstream.router.add_post('/v1/responses', mock_response)
        runner = web.AppRunner(upstream, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        upstream_port = runner.addresses[0][1]
        proxy_port = free_port()
        dashboard_port = free_port()
        while dashboard_port == proxy_port:
            dashboard_port = free_port()
        service = Service(Config(upstream=f'http://127.0.0.1:{upstream_port}',
                                 proxy_port=proxy_port, dashboard_port=dashboard_port,
                                 data_dir=str(Path(directory)/'data')))
        try:
            await service.start()
            await seed_demo(proxy_port)
            async with ClientSession() as client:
                base = f'http://127.0.0.1:{dashboard_port}'
                for path in ('/', '/app.js', '/style.css'):
                    async with client.get(base+path) as response:
                        assert response.status == 200, (path, response.status)
                        assert await response.read()
                async with client.get(base+'/api/captures') as response:
                    result = await response.json()
                    assert len(result['items']) == len(DEMO_SCENARIOS)
                async with client.get(base+'/api/export') as response:
                    assert response.status == 200
                    records = [json.loads(line) for line in (await response.text()).splitlines()]
                    assert len(records) == len(DEMO_SCENARIOS)
                    assert 'DEMO-DO-NOT-USE' not in json.dumps(records)
            print(f'Installed route-scope {__version__}: static assets, {len(records)} demo captures, export and redaction OK')
        finally:
            await service.close()
            await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
