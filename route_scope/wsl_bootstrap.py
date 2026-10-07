"""Standalone stdlib launcher used by Windows; does not alter WSL system Python."""
import os
from pathlib import Path
import pwd
import subprocess
import sys
import urllib.request


def main():
    project=Path(__file__).resolve().parents[1]
    users=sorted((u for u in pwd.getpwall() if 1000<=u.pw_uid<60000 and Path(u.pw_dir).is_dir()),key=lambda u:u.pw_uid)
    base=Path(users[0].pw_dir) if users else Path.home()
    candidates=[]
    for user in users:
        for name in ('venv','auto-venv','test-env'):
            candidates.append(Path(user.pw_dir)/'.local/share/route-scope'/name/'bin/python')
    python=None
    for candidate in candidates:
        if candidate.is_file():
            try:
                check=subprocess.run([str(candidate),'-c','import aiohttp, brotli, zstandard'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
                if check.returncode==0:python=candidate;break
            except (OSError,subprocess.TimeoutExpired):pass
    if python is None:
        env=base/'.local/share/route-scope/auto-venv'
        print('Preparing isolated Route Scope WSL environment.',flush=True)
        setup=subprocess.run([sys.executable,'-m','venv',str(env)],stdout=sys.stderr,stderr=sys.stderr)
        if setup.returncode:
            bootstrap=base/'.cache/route-scope/virtualenv.pyz'
            bootstrap.parent.mkdir(parents=True,exist_ok=True)
            urllib.request.urlretrieve('https://bootstrap.pypa.io/virtualenv.pyz',bootstrap)
            subprocess.run([sys.executable,str(bootstrap),str(env)],check=True,stdout=sys.stderr)
        python=env/'bin/python'
        subprocess.run([str(python),'-m','pip','install','--disable-pip-version-check',str(project)],check=True,stdout=sys.stderr)
    os.environ['PYTHONPATH']=str(project)
    os.chdir(project)
    os.execv(str(python),[str(python),'-m','route_scope.autocapture',*sys.argv[1:]])


if __name__=='__main__':main()
