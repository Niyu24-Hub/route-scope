"""Local-only demonstration of final-echo admission and bounded alternate-route use."""
import asyncio
import os

from aiohttp import ClientSession, web

from .cli import serve
from .demo import sse
from .server import Config


async def run(args):
    if not 1<=args.base_port<=65532:raise ValueError('base-port 必须在 1..65532')
    proxy_port,ui_port,first_port,second_port=range(args.base_port,args.base_port+4)
    os.environ['ROUTE_SCOPE_DEMO_ALT_KEY']='DEMO-LOCAL-KEY-NOT-A-REAL-SECRET'
    runners=[]
    for index,port in enumerate((first_port,second_port)):
        async def reply(request,index=index):
            payload=await request.json();requested=payload['reasoning']['effort']
            first={'id':'resp_demo_'+str(index),'model':'demo-model','status':'in_progress','reasoning':{'effort':'high' if index==0 else requested}}
            final={**first,'status':'completed','reasoning':{'effort':'low' if index==0 else requested},
                   'usage':{'output_tokens_details':{'reasoning_tokens':123}},
                   'output':[{'content':[{'type':'output_text','text':'LOCAL DEMO: this is not a real AnyRouter response.'}]}]}
            result=web.StreamResponse(headers={'Content-Type':'text/event-stream'})
            await result.prepare(request);await result.write(sse('response.created',first))
            await asyncio.sleep(.05);await result.write(sse('response.completed',final));await result.write_eof()
            return result
        app=web.Application();app.router.add_post('/v1/responses',reply)
        runner=web.AppRunner(app,access_log=None);await runner.setup();await web.TCPSite(runner,'127.0.0.1',port).start();runners.append(runner)
    config=Config(upstream=f'http://127.0.0.1:{first_port}',proxy_port=proxy_port,dashboard_port=ui_port,data_dir=args.data_dir,
                  guard={'models':['demo-model'],'minimum_effort':'high','raise_effort':True,'max_attempts':2,'retry_stateless':True},
                  routes=[{'name':'DEMO-primary','upstream':f'http://127.0.0.1:{first_port}'},
                          {'name':'DEMO-alternative','upstream':f'http://127.0.0.1:{second_port}','key_env':'ROUTE_SCOPE_DEMO_ALT_KEY'}])
    async def seed():
        await asyncio.sleep(.5)
        async with ClientSession() as client:
            for effort in ('xhigh','low'):
                async with client.post(f'http://127.0.0.1:{proxy_port}/v1/responses',json={'model':'demo-model','input':'LOCAL DEMO','reasoning':{'effort':effort},'stream':True}) as response:
                    await response.read()
        print('Local guard demo ready: first echo differs from final; bad route rejected and explicit alternate accepted. No AnyRouter calls.',flush=True)
    task=asyncio.create_task(seed())
    try:await serve(config)
    finally:
        if not task.done():task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        for runner in runners:await runner.cleanup()
