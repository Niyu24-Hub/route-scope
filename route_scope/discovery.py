"""Read-only discovery of CC Switch listeners and observable local sessions."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import subprocess
import time


def configured_ports(database):
    path = Path(database)
    if not path.is_file():
        return []
    try:
        with sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=1) as db:
            cols = {r[1] for r in db.execute('pragma table_info(proxy_config)')}
            if 'listen_port' not in cols:
                return []
            return sorted({int(r[0]) for r in db.execute('select listen_port from proxy_config') if 1 <= int(r[0]) <= 65535})
    except (sqlite3.Error, ValueError, TypeError):
        return []


def tcp_rows(proc=Path('/proc')):
    rows = []
    for name in ('tcp', 'tcp6'):
        try:
            lines = (proc/'net'/name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            try:
                addr, port = f[1].split(':')
                # IPv4 loopback/wildcard, and IPv6 loopback/wildcard listeners.
                local = addr in ('0100007F', '00000000', '00000000000000000000000001000000', '00000000000000000000000000000000')
                if local:
                    rows.append({'port':int(port,16), 'state':f[3], 'inode':f[9], 'family':name})
            except (ValueError, IndexError):
                continue
    return rows


class LinuxDiscovery:
    def __init__(self, salt, proc=Path('/proc'), interval=5):
        self.salt, self.proc, self.interval = salt, Path(proc), interval
        self.next_scan = 0
        self.ports = set(); self.processes = []; self.owners = {}; self.sessions = {}
        self.interfaces = []

    def scan(self, force=False):
        if not force and time.monotonic() < self.next_scan:
            return self
        self.next_scan = time.monotonic() + self.interval
        switches, clients, sessions, processes = set(), {}, {}, []
        try:
            entries = list(self.proc.iterdir())
        except OSError:
            entries = []
        for p in entries:
            if not p.name.isdigit():
                continue
            try:
                name = (p/'comm').read_text().strip()
                if name.lower() not in ('cc-switch','cc_switch','codex','claude'):
                    continue
                start = (p/'stat').read_text().rsplit(')',1)[1].split()[19]
                processes.append({'pid':int(p.name), 'name':name, 'start_ticks':start})
                for fd in (p/'fd').iterdir():
                    try:
                        target = str(fd.readlink())
                    except OSError:
                        continue
                    if target.startswith('socket:['):
                        inode = target[8:-1]
                        if name.lower() in ('cc-switch','cc_switch'):
                            switches.add(inode)
                        else:
                            clients.setdefault(inode, []).append(int(p.name))
                    elif name.lower() in ('codex','claude') and 'rollout-' in target:
                        match = re.search(r'([0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})\.jsonl$', target)
                        if match:
                            fp = hmac.new(self.salt,match[1].encode(),hashlib.sha256).hexdigest()[:16]
                            sessions.setdefault(fp, []).append(int(p.name))
            except (OSError, IndexError):
                continue
        rows = tcp_rows(self.proc)
        self.ports = {r['port'] for r in rows if r['inode'] in switches and r['state']=='0A'}
        self.owners = {r['port']:sorted(set(clients[r['inode']])) for r in rows if r['inode'] in clients}
        self.processes, self.sessions = processes, sessions
        self.interfaces = [name for _, name in socket.if_nameindex()]
        return self

    def attribute(self, record):
        pid = self.owners.get(record.get('client_port'), [])
        if pid:
            record.update(client_pids=pid, process_attribution='socket_inode')
        verified = sorted(set(self.sessions.get(record.get('session'), [])))
        if verified:
            record.update(verified_session_processes=verified, session_attribution='open_rollout_filename', attribution_epoch=time.time())


class WindowsDiscovery:
    def __init__(self, salt, interval=8):
        self.salt, self.interval = salt, interval
        self.next_scan = 0; self.ports = set(); self.processes = []; self.owners = {}; self.interfaces = []
        self.pending = None; self.scan_started = 0

    def scan(self, force=False):
        if self.pending is not None:
            if self.pending.poll() is None:
                if time.monotonic()-self.scan_started>12:
                    self.close()
                return self
            run=self.pending;self.pending=None
            output,_=run.communicate()
            try:
                value=json.loads(output.decode('utf-8-sig',errors='replace'))
                self.ports={int(x) for x in value.get('ports',[]) if 1<=int(x)<=65535}
                self.processes=[{'pid':x['Id'],'name':x['ProcessName']} for x in value.get('processes',[])]
            except (ValueError,TypeError,KeyError):
                pass
        if not force and time.monotonic() < self.next_scan:
            return self
        self.next_scan = time.monotonic() + self.interval
        # No command line or process environment is read; API keys cannot appear.
        script = '''$p=@(Get-Process -Name cc-switch,codex,claude -ErrorAction SilentlyContinue | Select-Object Id,ProcessName); $ids=@($p | Where-Object {$_.ProcessName -eq 'cc-switch'} | ForEach-Object {$_.Id}); $ports=@(Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object {$_.OwningProcess -in $ids} | Select-Object -ExpandProperty LocalPort -Unique); @{ports=$ports;processes=$p} | ConvertTo-Json -Compress -Depth 4'''
        try:
            exe = str(Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe')
            # Discovery must not stop draining Npcap while PowerShell enumerates sockets.
            self.pending = subprocess.Popen([exe,'-NoProfile','-NonInteractive','-Command',script],
                stdout=subprocess.PIPE,stderr=subprocess.PIPE,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            self.scan_started=time.monotonic()
        except OSError:
            pass
        return self

    def close(self):
        if self.pending is not None:
            if self.pending.poll() is None:self.pending.kill()
            self.pending.communicate();self.pending=None

    def attribute(self, record):
        return None  # Do not invent PID attribution from a port number on Windows.
