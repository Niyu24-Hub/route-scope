"""Runtime state for managed capture processes; never manages user processes."""
import json
import os
from pathlib import Path
import time


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    try:
        os.replace(temp, path)
    except PermissionError:
        temp.unlink(missing_ok=True)  # A Windows reader may briefly hold the file.


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


class InstanceLock:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b'0'); self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close(); self.file = None
            raise RuntimeError('该数据目录已有抓取服务运行，请使用现有面板或另选数据目录') from None
        return self

    def __exit__(self, *args):
        if self.file:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_UN)
            self.file.close(); self.file = None


def stop_requested(root, run_id):
    return read_json(Path(root)/'control.json').get('stop_run_id') == run_id


def supervisor_gone(status, run_id, now=None):
    now=time.time() if now is None else now
    return (not status or status.get('run_id')!=run_id or status.get('state') in ('stopping','stopped')
            or now-status.get('updated_epoch',0)>90)


def runtime_state(directory):
    root = Path(directory)
    status = read_json(root/'watch-status.json')
    if not status:
        return None
    now = time.time()
    status['stale'] = now - status.get('updated_epoch', 0) > 15
    status['workers'] = []
    for name in status.get('worker_dirs', []):
        # Only direct children of the capture root may be read.
        if Path(name).name != name or name in ('.', '..'):
            continue
        worker = read_json(root/name/'capture-status.json')
        if not worker:
            worker = {'name': name, 'state': 'starting'}
        worker['stale'] = now - worker.get('updated_epoch', 0) > 15 or worker.get('run_id')!=status.get('run_id')
        child=next((c for c in status.get('supervisor',[]) if c.get('directory')==name),{})
        worker['restart_count']=child.get('restarts',0)
        if not child.get('running',True) and status.get('state')=='running':
            worker['state']='recovering'
            worker['error']='抓取子进程退出，管理器正在自动重启；详情见该来源的 worker.log'
        status['workers'].append(worker)
    status['paused'] = bool(read_json(root/'control.json').get('paused'))
    return status


def prepare_watch(directory, timeout=30):
    """Reuse the current version, or cooperatively stop an obsolete supervisor."""
    from . import __version__
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    status = read_json(root/'watch-status.json')
    fresh = time.time() - status.get('updated_epoch', 0) < 15
    if status.get('state') == 'running' and fresh and status.get('version') == __version__:
        return {'action': 'reuse', 'dashboard_url': status.get('dashboard_url'), 'version': __version__}
    restarting = status.get('state') in ('running', 'starting', 'stopping')
    if restarting and status.get('run_id'):
        atomic_json(root/'control.json', {'stop_run_id': status['run_id'], 'paused': False})
    deadline = time.monotonic() + timeout
    while True:
        try:
            with InstanceLock(root/'watch.lock'):
                return {'action': 'restart' if restarting else 'start', 'version': __version__}
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise RuntimeError('旧抓取服务尚未退出；已通知停止，请稍后重试。未强制终止任何进程。') from None
            time.sleep(.2)
