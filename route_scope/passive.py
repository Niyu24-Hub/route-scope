"""Linux loopback HTTP/1 observation. Receives packet copies; never sends packets.

No ptrace, firewall rules, routes, TLS injection, or process configuration changes.
This observes the client <-> CC Switch boundary, not encrypted upstream traffic.
"""
import argparse
from collections import deque
import hashlib
import json
from pathlib import Path
import socket
import struct
import time
from urllib.parse import urlsplit

from .evidence import Observation
from .store import Store


class TCPStream:
    def __init__(self, consume, initial=None):
        self.consume = consume
        self.next = initial
        self.waiting = {}

    def feed(self, seq, data):
        if not data:
            return
        if self.next is None:
            self.next = seq
        delta = (seq - self.next + 2**31) % 2**32 - 2**31
        if delta > 0:
            self.waiting[seq] = data
            if sum(len(x) for x in self.waiting.values()) > 8*1024*1024:
                raise ValueError('tcp_reassembly_limit')
            return
        if -delta >= len(data):
            return
        data = data[-delta:]
        self.next = (self.next + len(data)) % 2**32
        self.consume(data)
        while self.waiting:
            ready = next((s for s in self.waiting if (s-self.next+2**31)%2**32-2**31 <= 0), None)
            if ready is None:
                break
            self.feed(ready, self.waiting.pop(ready))


class HTTPStream:
    def __init__(self, is_request, start, data, end):
        self.is_request, self.start, self.data, self.end = is_request, start, data, end
        self.buffer = bytearray()
        self.state = 'headers'
        self.remaining = 0
        self.synced = False
        self.ignored_prefix = False

    def feed(self, data):
        if not self.synced:
            if data.startswith(b'PRI * HTTP/2.0'):
                raise ValueError('passive_http2_not_supported')
            if len(data)>=3 and data[0] in (0x16,0x17) and data[1]==3 and data[2]<=4:
                raise ValueError('passive_tls_not_supported')
            prefixes = (b'POST ', b'GET ', b'PUT ', b'PATCH ', b'DELETE ', b'HEAD ', b'OPTIONS ') if self.is_request else (b'HTTP/1.',)
            if not any(data.startswith(p) or p.startswith(data) for p in prefixes):
                self.ignored_prefix = True
                return
            self.synced = True
        self.buffer.extend(data)
        while True:
            if self.state == 'headers':
                end = self.buffer.find(b'\r\n\r\n')
                if end < 0:
                    if len(self.buffer) > 256*1024:
                        raise ValueError('http_header_limit')
                    return
                head = bytes(self.buffer[:end]).decode('iso-8859-1')
                del self.buffer[:end+4]
                lines = head.split('\r\n')
                headers = {}
                for line in lines[1:]:
                    key, sep, value = line.partition(':')
                    if not sep:
                        raise ValueError('invalid_header')
                    key, value = key.lower(), value.strip()
                    if key in headers and key in ('content-length','transfer-encoding') and headers[key] != value:
                        raise ValueError('ambiguous_framing')
                    headers[key] = value
                parts = lines[0].split(' ')
                if self.is_request:
                    if len(parts) != 3 or not parts[2].startswith('HTTP/1.'):
                        raise ValueError('not_http1_request')
                    first = (parts[0], urlsplit(parts[1]).path)
                    empty = False
                else:
                    if len(parts) < 2 or not parts[0].startswith('HTTP/1.'):
                        raise ValueError('not_http1_response')
                    first = int(parts[1])
                    if first == 101:
                        raise ValueError('passive_websocket_not_supported')
                    if 100 <= first < 200:
                        continue
                    empty = first in (204,304)
                empty = bool(self.start(first, headers)) or empty
                if empty:
                    self.end()
                    continue
                if 'chunked' in headers.get('transfer-encoding','').lower():
                    self.state = 'chunk_size'
                elif 'content-length' in headers:
                    self.remaining = int(headers['content-length'])
                    if self.remaining < 0:
                        raise ValueError('invalid_length')
                    self.state = 'length'
                elif self.is_request:
                    self.end()
                else:
                    self.state = 'close'
            elif self.state == 'length':
                n = min(self.remaining,len(self.buffer))
                if n:
                    self.data(bytes(self.buffer[:n])); del self.buffer[:n]; self.remaining -= n
                if self.remaining:
                    return
                self.end(); self.state = 'headers'
            elif self.state == 'chunk_size':
                end = self.buffer.find(b'\r\n')
                if end < 0:
                    if len(self.buffer)>128: raise ValueError('chunk_header_limit')
                    return
                self.remaining = int(self.buffer[:end].split(b';',1)[0],16)
                if self.remaining < 0: raise ValueError('invalid_chunk_size')
                del self.buffer[:end+2]
                self.state = 'chunk_body' if self.remaining else 'trailers'
            elif self.state == 'chunk_body':
                n = min(self.remaining,len(self.buffer))
                if n:
                    self.data(bytes(self.buffer[:n])); del self.buffer[:n]; self.remaining -= n
                if self.remaining:
                    return
                self.state = 'chunk_crlf'
            elif self.state == 'chunk_crlf':
                if len(self.buffer)<2: return
                if self.buffer[:2] != b'\r\n': raise ValueError('invalid_chunk_end')
                del self.buffer[:2]; self.state='chunk_size'
            elif self.state == 'trailers':
                end = self.buffer.find(b'\r\n')
                if end<0:
                    if len(self.buffer)>256*1024: raise ValueError('trailer_limit')
                    return
                del self.buffer[:end+2]
                if end == 0:
                    self.end(); self.state='headers'
            elif self.state == 'close':
                self.data(bytes(self.buffer)); self.buffer.clear(); return
            if not self.buffer and self.state != 'length':
                return

    def eof(self):
        if self.state == 'close':
            self.end(); self.state='headers'


def process_snapshot(pids):
    result = []
    for pid in pids:
        proc = Path('/proc')/str(pid)
        try:
            result.append({'pid':pid, 'comm':(proc/'comm').read_text().strip(),
                'start_ticks':(proc/'stat').read_text().rsplit(')',1)[1].split()[19],
                'cwd':str((proc/'cwd').readlink())})
        except OSError:
            result.append({'pid':pid, 'state':'not_present'})
    return result


def socket_owners(port):
    inodes = set()
    try:
        tcp_table=Path('/proc/net/tcp').read_text()
    except OSError:
        return []
    for line in tcp_table.splitlines()[1:]:
        fields=line.split()
        if int(fields[1].split(':')[1],16)==port:
            inodes.add('socket:['+fields[9]+']')
    owners=[]
    if not inodes: return owners
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit(): continue
        try:
            if any(str(fd.readlink()) in inodes for fd in (proc/'fd').iterdir()):
                owners.append(int(proc.name))
        except (OSError, PermissionError): pass
    return owners


class Conversation:
    def __init__(self, store, client_port, counters, capture_port=15722, *, owners=None, source='wsl-passive', capture_bodies=True, annotate=None, quiet=False):
        self.store,self.port,self.counters=store,client_port,counters
        self.capture_port=capture_port
        self.queue=deque(); self.current=None; self.body=bytearray(); self.request=None
        self.last_save=0.; self.last_packet=time.monotonic()
        self.owners=socket_owners(client_port) if owners is None else owners
        self.source,self.capture_bodies,self.annotate,self.quiet=source,capture_bodies,annotate,quiet
        self.http=[HTTPStream(True,self.request_start,self.request_data,self.request_end),
                   HTTPStream(False,self.response_start,self.response_data,self.response_end)]
        self.tcp=[TCPStream(p.feed) for p in self.http]

    def save(self, obs):
        r=obs.refresh()
        if self.annotate:self.annotate(r)
        r['verdict_label']=r['verdict_label'].replace('上游回显','CC Switch 回显')
        self.store.save(r)

    def request_start(self, first, headers):
        self.request=(first,headers); self.body=bytearray()

    def request_data(self, data):
        self.body.extend(data)
        if len(self.body)>32*1024*1024: raise ValueError('request_body_limit')

    def request_end(self):
        (method,path),headers=self.request
        obs=Observation(method,path,headers,bytes(self.body),self.store.salt,source=self.source,capture=self.capture_bodies)
        obs.record.update(capture_boundary='client_to_ccswitch',capture_port=self.capture_port,
                          client_port=self.port,client_pids=self.owners,
                          process_attribution='socket_inode' if self.owners else 'unresolved',
                          upstream_request_observed=False)
        self.queue.append(obs); self.save(obs); self.counters['requests']+=1
        self.request=None; self.body.clear()

    def response_start(self, status, headers):
        self.current=self.queue.popleft() if self.queue else None
        if self.current:
            self.current.headers(status,headers); self.save(self.current)
            return self.current.record['method']=='HEAD'
        else:
            self.counters['responses_without_request']+=1

    def response_data(self, data):
        if self.current:
            self.current.feed(data)
            if time.monotonic()-self.last_save>1:
                self.save(self.current); self.last_save=time.monotonic()

    def response_end(self):
        if self.current:
            self.current.record['http_message_complete']=True
            self.current.finish(); self.save(self.current)
            r=self.current.record
            if not self.quiet:
                print(json.dumps({k:r.get(k) for k in ('id','client_pids','requested_model','requested','final','http_status','state','verdict','reasoning_tokens')},ensure_ascii=False),flush=True)
            self.current=None; self.counters['responses']+=1

    def close(self, reason=None):
        if reason is None:
            self.http[1].eof()
        pending=list(self.queue)+([self.current] if self.current else [])
        for obs in pending:
            # A capture boundary is not an upstream error. Many SSE clients reset
            # the connection immediately after response.completed.
            obs.record['http_message_complete']=False
            obs.record['capture_end_reason']=reason or 'capture_ended_before_response_end'
            if reason in ('capture_parse_error','capture_packet_loss'):
                obs.record['parse_errors'].append(reason)
            obs.record['terminal_event_observed']=bool(obs.record.get('terminal_event'))
            if obs.record['terminal_event_observed']:
                obs.disabled=True  # Event is already parsed; avoid re-decoding EOF.
            obs.finish(); self.save(obs)
        self.queue.clear(); self.current=None


def decode_packet(packet, port):
    if len(packet)<54 or packet[12:14]!=b'\x08\x00': return None
    ip=14; ihl=(packet[ip]&15)*4
    if packet[ip]>>4!=4 or packet[ip+9]!=6 or ihl<20: return None
    if packet[ip+12:ip+16]!=b'\x7f\x00\x00\x01' or packet[ip+16:ip+20]!=b'\x7f\x00\x00\x01': return None
    if struct.unpack_from('!H',packet,ip+6)[0]&0x3fff: return None
    tcp=ip+ihl
    if len(packet)<tcp+20: return None
    src,dst,seq=struct.unpack_from('!HHI',packet,tcp)
    if port not in (src,dst): return None
    hlen=(packet[tcp+12]>>4)*4
    if hlen<20: return None
    end=ip+struct.unpack_from('!H',packet,ip+2)[0]
    if end>len(packet): raise ValueError('packet_truncated')
    return (dst if src==port else src, 1 if src==port else 0, seq, packet[tcp+13], packet[tcp+hlen:end])


def capture(args):
    if not hasattr(socket,'AF_PACKET'): raise RuntimeError('passive capture requires Linux / WSL')
    store=Store(args.data_dir)
    before=process_snapshot(args.pid)
    stats={'requests':0,'responses':0,'responses_without_request':0,'packets':0,'parse_errors':0}
    flows={}; errors={}
    # ETH_P_ALL is required to receive outgoing frames on WSL mirrored devices.
    # Filter IPv4/TCP and the chosen port in decode_packet before retaining data.
    s=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3))
    s.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,8*1024*1024)
    s.bind((args.interface,0)); s.settimeout(.5)
    started=time.time(); deadline=time.monotonic()+args.seconds
    print(json.dumps({'event':'passive_started','port':args.port,'seconds':args.seconds,'processes':before}),flush=True)
    try:
        while time.monotonic()<deadline:
            try: packet,addr=s.recvfrom(131072)
            except socket.timeout: continue
            # WSL mirrored loopback0 uses outgoing frames for one half of a flow.
            # Keep both directions; TCP sequence reassembly removes lo duplicates.
            try:
                parsed=decode_packet(packet,args.port)
                if not parsed: continue
                peer,direction,seq,flags,payload=parsed; stats['packets']+=1
                if direction==0 and flags&2:
                    if peer in flows: flows.pop(peer).close('new_connection')
                if peer not in flows:
                    if not payload and not flags&2: continue
                    if len(flows)>=128:
                        oldest=min(flows,key=lambda p:flows[p].last_packet)
                        flows.pop(oldest).close('connection_limit')
                    flows[peer]=Conversation(store,peer,stats,args.port)
                flow=flows[peer]; flow.last_packet=time.monotonic()
                if flags&2: flow.tcp[direction].next=(seq+1)%2**32
                flow.tcp[direction].feed((seq+1)%2**32 if flags&2 else seq,payload)
                if flags&4 or (flags&1 and direction==1):
                    flow.close('connection_reset' if flags&4 else None); flows.pop(peer,None)
            except Exception as exc:
                name=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
                # Error names only; never output raw packet data or header contents.
                if len(name)>80: name=type(exc).__name__
                errors[name]=errors.get(name,0)+1;stats['parse_errors']+=1
                if 'peer' in locals() and peer in flows: flows.pop(peer).close('passive_parse_error')
    finally:
        for flow in flows.values(): flow.close('capture_window_ended')
        packets,dropped=struct.unpack('II',s.getsockopt(263,6,8))
        s.close()
        summary={'started_epoch':started,'duration_seconds':round(time.time()-started,2),'port':args.port,
                 'interface':args.interface,'capture_boundary':'client_to_ccswitch','network_or_process_config_changed':False,
                 'stats':stats,'socket_packets':packets,'socket_drops':dropped,'errors':errors,
                 'processes_before':before,'processes_after':process_snapshot(args.pid)}
        (Path(args.data_dir)/'passive-summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
        store.close(); print(json.dumps(summary,ensure_ascii=False),flush=True)


def main():
    p=argparse.ArgumentParser(description='WSL 回环 HTTP/1 旁路抓取；不修改原进程或路由')
    p.add_argument('--port',type=int,default=15722)
    p.add_argument('--seconds',type=int,default=120)
    p.add_argument('--interface',default='lo',help='WSL mirrored 网络通常使用 loopback0')
    p.add_argument('--pid',type=int,action='append',default=[],help='仅核验进程存活及启动标识，不过滤流量')
    p.add_argument('--data-dir',required=True)
    args=p.parse_args()
    if not 1<=args.port<=65535 or not 1<=args.seconds<=3600: p.error('invalid port/duration')
    capture(args)


if __name__=='__main__': main()
