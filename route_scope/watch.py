"""One-command supervisor for automatic passive capture and its local dashboard."""
import asyncio
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import uuid
import webbrowser

from .catalog import Catalog
from .runtime import InstanceLock, atomic_json, read_json, stop_requested
from .server import Config, Service

NO_WINDOW=getattr(subprocess,'CREATE_NO_WINDOW',0)


def decode_wsl_output(data):
    return data.decode('utf-16-le' if b'\0' in data else 'utf-8-sig',errors='replace').replace('\0','').strip()


def wsl_exe():
    return str(Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/wsl.exe')


def distributions():
    # Read installed names; never stop or terminate a distro to configure capture.
    run=subprocess.run([wsl_exe(),'--list','--quiet'],capture_output=True,timeout=15,creationflags=NO_WINDOW)
    if run.returncode:return []
    return [x.strip() for x in decode_wsl_output(run.stdout).splitlines() if x.strip() and not x.lower().startswith('docker-desktop')]


def linux_path(path, distro):
    run=subprocess.run([wsl_exe(),'-d',distro,'--exec','wslpath','-a','-u',Path(path).resolve().as_posix()],
                       capture_output=True,timeout=20,creationflags=NO_WINDOW)
    value=decode_wsl_output(run.stdout)
    if run.returncode or not value.startswith('/'):
        raise RuntimeError('WSL 无法访问当前项目路径，请从本机磁盘启动或在 WSL 内启动')
    return value


def commands(args, run_id):
    root=Path(args.data_dir).resolve()
    common=['--run-id',run_id,'--retention',str(args.retention)]
    if args.metadata_only:common+=['--metadata-only']
    for port in args.port:common+=['--port',str(port)]
    jobs=[];errors=[]
    if os.name!='nt' or not args.wsl_only:
        name='windows' if os.name=='nt' else 'linux'
        jobs.append({'directory':name,'label':'Windows 本机' if os.name=='nt' else 'WSL / Linux',
            'command':[sys.executable,'-m','route_scope.autocapture','--data-dir',str(root/name),
                       '--control-dir',str(root),'--label',name,*common]})
    if os.name=='nt' and not args.native_only:
        try:
            names=[args.distro] if args.distro else distributions()
            if not names:errors.append('未发现可用 WSL；请查看 Windows 捕获覆盖状态')
            for name in names:
                try:
                    folder='wsl-'+hashlib.sha256(name.encode()).hexdigest()[:10]
                    linux_root=linux_path(root,name)
                    bootstrap=linux_path(Path(__file__).with_name('wsl_bootstrap.py'),name)
                    jobs.append({'directory':folder,'label':'WSL / '+name,'distro':name,
                        'command':[wsl_exe(),'-d',name,'-u','root','--exec','python3',bootstrap,
                            '--data-dir',linux_root+'/'+folder,'--control-dir',linux_root,'--label','WSL / '+name,*common]})
                except (OSError,RuntimeError,subprocess.TimeoutExpired) as exc:
                    errors.append('WSL '+name+' 接入失败：'+(str(exc) if isinstance(exc,RuntimeError) else type(exc).__name__))
        except (OSError,subprocess.TimeoutExpired):
            errors.append('WSL 启动器不可用')
    return jobs,errors


class Child:
    def __init__(self, job, root):
        self.job,self.root=job,root
        self.process=None;self.log=None;self.restarts=0;self.retry_at=0.;self.started=False

    def start(self):
        folder=self.root/self.job['directory'];folder.mkdir(parents=True,exist_ok=True)
        logpath=folder/'worker.log'
        if logpath.exists() and logpath.stat().st_size>1024*1024:
            os.replace(logpath,folder/'worker.previous.log')
        self.log=logpath.open('ab')
        try:
            self.process=subprocess.Popen(self.job['command'],cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.DEVNULL,stdout=self.log,stderr=self.log,creationflags=NO_WINDOW)
            if self.started:self.restarts+=1
            self.started=True
        except OSError:
            self.log.close();self.log=None;self.retry_at=time.monotonic()+10
            raise

    def tick(self):
        if self.process is not None and self.process.poll() is not None:
            if self.log:self.log.close();self.log=None
            self.process=None
            self.retry_at=time.monotonic()+min(30,2**min(self.restarts+1,4))
        if self.process is None and time.monotonic()>=self.retry_at:
            self.start()

    def close_log(self):
        if self.log:self.log.close();self.log=None


async def run_watch(args):
    root=Path(args.data_dir).expanduser().resolve();root.mkdir(parents=True,exist_ok=True)
    args.data_dir=str(root)
    run_id=uuid.uuid4().hex
    with InstanceLock(root/'watch.lock'):
        jobs,setup_errors=await asyncio.to_thread(commands,args,run_id)
        if not jobs:raise RuntimeError('没有可启动的抓取后端：'+'；'.join(setup_errors))
        atomic_json(root/'control.json',{'paused':False})
        children=[Child(job,root) for job in jobs]
        started=time.time();stop=asyncio.Event();loop=asyncio.get_running_loop()
        previous={}
        for sig in (signal.SIGINT,signal.SIGTERM):
            previous[sig]=signal.getsignal(sig)
            signal.signal(sig,lambda *_:loop.call_soon_threadsafe(stop.set))
        status={'run_id':run_id,'pid':os.getpid(),'state':'starting','started_epoch':started,
                'worker_dirs':[j['directory'] for j in jobs],'mode':'automatic_passive',
                'dashboard_url':f'http://127.0.0.1:{args.dashboard_port}',
                'configuration_changed':False,'setup_errors':setup_errors}
        def publish(state):
            status.update(state=state,updated_epoch=time.time(),supervisor=[{
                'name':c.job['label'],'directory':c.job['directory'],'restarts':c.restarts,
                'running':bool(c.process and c.process.poll() is None)} for c in children])
            atomic_json(root/'watch-status.json',status)
        publish('starting')
        service=Service(Config(data_dir=str(root),dashboard_port=args.dashboard_port,
            retention=args.retention,capture_bodies=not args.metadata_only),viewer=True,store_override=Catalog(root,args.retention))
        try:
            await service.start()
            for child in children:
                try:child.start()
                except OSError:setup_errors.append(child.job['label']+' 启动失败，将自动重试')
            print('自动旁路抓取已启动：'+status['dashboard_url']+'\n保持本窗口运行；正常使用 Codex/Claude 即会自动记录。Ctrl+C 只停止本项目。',flush=True)
            if args.open_browser:await asyncio.to_thread(webbrowser.open,status['dashboard_url'])
            while not stop.is_set() and not stop_requested(root,run_id):
                for child in children:
                    try:child.tick()
                    except OSError:pass
                publish('running')
                try:await asyncio.wait_for(stop.wait(),timeout=1)
                except asyncio.TimeoutError:pass
        finally:
            atomic_json(root/'control.json',{'stop_run_id':run_id,'paused':False})
            publish('stopping')
            # Cooperative marker reaches the WSL worker too. No signal is sent to
            # Codex, CC Switch, mihomo or the WSL distribution.
            deadline=time.monotonic()+20
            while any(c.process and c.process.poll() is None for c in children) and time.monotonic()<deadline:
                await asyncio.sleep(.2)
            lingering=[c.job['label'] for c in children if c.process and c.process.poll() is None]
            if lingering:status['shutdown_note']='已通知抓取器停止，仍在收尾：'+', '.join(lingering)
            for child in children:child.close_log()
            await service.close()
            publish('stopped')
            for sig,handler in previous.items():signal.signal(sig,handler)


def request_stop(directory):
    root=Path(directory).expanduser().resolve();status=read_json(root/'watch-status.json')
    if not status or status.get('state')=='stopped':return {'state':'not_running'}
    atomic_json(root/'control.json',{'stop_run_id':status['run_id'],'paused':False})
    return {'state':'stop_requested','run_id':status['run_id']}
