"""Run with python -I to verify the installed wheel, independent of checkout imports."""
import asyncio
import argparse
import json
from pathlib import Path
import socket
import tempfile

from aiohttp import ClientSession, web
from route_scope import __version__
from route_scope.demo import DEMO_COUNT, mock_response, mock_message, seed_demo
from route_scope.server import Config, Service


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


async def main(browser=False, screenshots=None):
    with tempfile.TemporaryDirectory(prefix='route-scope-smoke-') as directory:
        upstream = web.Application()
        upstream.router.add_post('/v1/responses', mock_response)
        upstream.router.add_post('/v1/messages', mock_message)
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
                    assert len(result['items']) == DEMO_COUNT
                    claude = [r for r in result['items'] if r.get('protocol') == 'anthropic_messages']
                    assert len(claude) == 4
                    assert any(r['thinking_tokens'] == 0 and r['verdict'] == 'no_echo' for r in claude)
                async with client.get(base+'/api/export') as response:
                    assert response.status == 200
                    records = [json.loads(line) for line in (await response.text()).splitlines()]
                    assert len(records) == DEMO_COUNT
                    assert 'DEMO-DO-NOT-USE' not in json.dumps(records)
            if browser:
                from playwright.async_api import async_playwright, expect
                async with async_playwright() as playwright:
                    chromium = await playwright.chromium.launch()
                    try:
                        page = await chromium.new_page(viewport={'width': 1600, 'height': 1050})
                        errors = []
                        page.on('pageerror', lambda error: errors.append(str(error)))
                        await page.goto(base)
                        await expect(page.locator('#total')).to_have_text(str(DEMO_COUNT))
                        await page.click('#pause')
                        assert await page.locator('#no-echo').inner_text() == '3'
                        await page.select_option('#clients', 'claude')
                        assert await page.locator('#rows tr').count() == 4
                        text = await page.locator('#rows').inner_text()
                        assert '2,927' in text and 'thinking.type' in text and '标准协议无回显' in text
                        await page.click('[data-filter="unknown"]')
                        assert await page.locator('#rows tr').count() == 1
                        assert '消息级' in await page.locator('#rows').inner_text()
                        await page.click('[data-filter="no_echo"]')
                        assert await page.locator('#rows tr').count() == 3
                        await page.locator('#rows .detail-button').first.click()
                        await page.locator('#detail[open]').wait_for(state='visible')
                        detail = await page.locator('#detail-content').inner_text()
                        assert 'thinking_tokens' in detail and 'reasoning_tokens_source' in detail
                        await page.click('#detail-close')
                        await page.click('[data-filter="all"]')
                        if screenshots:
                            Path(screenshots).mkdir(parents=True, exist_ok=True)
                            await page.screenshot(path=str(Path(screenshots)/'claude-demo.png'), full_page=True)
                        await page.set_viewport_size({'width': 390, 'height': 844})
                        assert await page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        if screenshots:
                            await page.screenshot(path=str(Path(screenshots)/'claude-mobile.png'), full_page=True)
                        assert not errors, errors
                        print('Chromium: Claude filters, no-echo counts, token details and mobile layout OK')
                    finally:
                        await chromium.close()
            print(f'Installed route-scope {__version__}: static assets, {len(records)} demo captures, export and redaction OK')
        finally:
            await service.close()
            await runner.cleanup()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--screenshots')
    args = parser.parse_args()
    asyncio.run(main(args.browser, args.screenshots))
