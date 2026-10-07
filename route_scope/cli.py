import argparse
import asyncio
import json
import signal
import sys
import tomllib
from pathlib import Path

from . import ccswitch
from .server import Config, Service
from . import __version__


def print_json(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


async def serve(config, viewer=False):
    service = Service(config, viewer=viewer)
    await service.start()
    print(f"模式: {'只读面板' if viewer else '代理与面板'}\n面板: http://127.0.0.1:{config.dashboard_port}", flush=True)
    if not viewer:
        print(f"代理: http://{config.proxy_host}:{config.proxy_port}/v1\n上游: {config.upstream}\nCtrl+C 停止。停止前请恢复 CC Switch 原地址。", flush=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        previous[sig] = signal.getsignal(sig)
        signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    try:
        await stop.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        await service.close()


def parser():
    p = argparse.ArgumentParser(description="CC Switch / AnyRouter 请求与响应证据观测")
    p.add_argument('--version', action='version', version=f'route-scope {__version__}')
    sub = p.add_subparsers(dest="command", required=True)
    capture_doctor=sub.add_parser('capture-doctor',help='只读诊断 Windows 回环捕获能力，不修改驱动')
    capture_doctor.add_argument('--open-interface',action='store_true',help='额外打开并关闭本项目捕获句柄，检查权限')
    watch = sub.add_parser('watch',help='自动发现 CC Switch 并持续旁路抓取，启动统一面板')
    watch.add_argument('--data-dir',default='data/auto')
    watch.add_argument('--dashboard-port',type=int,default=15927)
    watch.add_argument('--distro',help='Windows 下指定 WSL 发行版；默认发现所有非 Docker 发行版')
    mode=watch.add_mutually_exclusive_group()
    mode.add_argument('--wsl-only',action='store_true')
    mode.add_argument('--native-only',action='store_true')
    watch.add_argument('--port',type=int,action='append',default=[],help='额外观察的本机端口，可重复；默认只发现 CC Switch 自己的监听端口')
    watch.add_argument('--retention',type=int,default=1000)
    watch.add_argument('--metadata-only',action='store_true')
    watch.add_argument('--open-browser',action='store_true')
    for command in ('watch-status','watch-stop'):
        ctl=sub.add_parser(command,help='查看自动抓取状态' if command=='watch-status' else '仅停止本项目自动抓取服务')
        ctl.add_argument('--data-dir',default='data/auto')
    s = sub.add_parser("serve", help="启动本地代理和中文面板")
    s.add_argument("--config")
    s.add_argument("--upstream")
    s.add_argument("--proxy-port", type=int)
    s.add_argument("--dashboard-port", type=int)
    s.add_argument("--outbound-proxy")
    s.add_argument("--data-dir")
    s.add_argument("--metadata-only", action="store_true")
    v = sub.add_parser("view", help="只启动面板，读取已有抓包或 mitmproxy 记录")
    v.add_argument("--data-dir", default="data/mitm")
    v.add_argument("--dashboard-port", type=int, default=15924)
    d = sub.add_parser("doctor", help="只读检查本机 CC Switch")
    d.add_argument("--db", default=str(Path.home()/".cc-switch"/"cc-switch.db"))
    route = sub.add_parser("connect", help="预览/接入一个 Codex 供应商；修改前退出 CC Switch")
    route.add_argument("--db", default=str(Path.home()/".cc-switch"/"cc-switch.db"))
    route.add_argument("--provider", required=True)
    route.add_argument("--url", default="http://127.0.0.1:15723/v1")
    route.add_argument("--receipt", default="data/route-receipt.json")
    route.add_argument("--apply", action="store_true")
    restore = sub.add_parser("restore", help="按接入记录恢复原 URL；修改前退出 CC Switch")
    restore.add_argument("--receipt", default="data/route-receipt.json")
    restore.add_argument("--apply", action="store_true")
    demo = sub.add_parser("demo", help="启动本地模拟上游、生成示例抓包并打开面板服务")
    demo.add_argument("--data-dir", default="data/demo")
    demo.add_argument("--proxy-port", type=int, default=15823)
    demo.add_argument("--dashboard-port", type=int, default=15824)
    demo.add_argument("--upstream-port", type=int, default=15825)
    gd=sub.add_parser('guard-demo',help='本地演示首包/最终回显核验和有限备用路由，不请求真实模型')
    gd.add_argument('--data-dir',default='data/guard-demo')
    gd.add_argument('--base-port',type=int,default=15833,help='演示使用从该端口起的四个本机端口')
    return p


def main():
    args = parser().parse_args()
    try:
        if args.command == 'capture-doctor':
            from .windows_diagnostics import inventory
            print_json(inventory(args.open_interface))
        elif args.command == 'watch':
            from .watch import run_watch
            if args.retention<1 or any(not 1<=p<=65535 for p in args.port):raise ValueError('无效的端口或保留数量')
            if sys.platform!='win32' and args.wsl_only:raise ValueError('--wsl-only 用于 Windows 启动器；WSL 内直接运行 watch')
            asyncio.run(run_watch(args))
        elif args.command == 'watch-status':
            from .runtime import runtime_state
            print_json(runtime_state(args.data_dir) or {'state':'not_running'})
        elif args.command == 'watch-stop':
            from .watch import request_stop
            print_json(request_stop(args.data_dir))
        elif args.command == "serve":
            values = {}
            if args.config:
                values = tomllib.loads(Path(args.config).read_text(encoding="utf-8-sig"))
            for key in ("upstream", "proxy_port", "dashboard_port", "outbound_proxy", "data_dir"):
                if getattr(args, key) is not None:
                    values[key] = getattr(args, key)
            if args.metadata_only:
                values["capture_bodies"] = False
            asyncio.run(serve(Config(**values)))
        elif args.command == "doctor":
            print_json({"python": sys.version.split()[0], "platform": sys.platform, "database": args.db, "providers": ccswitch.inspect(args.db)})
        elif args.command == "view":
            asyncio.run(serve(Config(data_dir=args.data_dir, dashboard_port=args.dashboard_port), viewer=True))
        elif args.command == "connect":
            print_json(ccswitch.change(args.db, args.provider, args.url, args.receipt, args.apply))
            print("已修改；重启 CC Switch，确认代理启用。" if args.apply else "仅预览。退出 CC Switch 后追加 --apply 可接入。")
        elif args.command == "restore":
            print_json(ccswitch.restore(args.receipt, args.apply))
        elif args.command == "demo":
            from .demo import run_demo
            asyncio.run(run_demo(args))
        elif args.command == 'guard-demo':
            from .guard_demo import run
            asyncio.run(run(args))
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, TypeError, RuntimeError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        raise SystemExit(1)
