"""Continuous passive capture worker; discovery and control are receive-only."""
import argparse
from collections import Counter
import os
from pathlib import Path
import signal
import time

from .discovery import LinuxDiscovery, WindowsDiscovery
from .packet_sources import CaptureUnavailable, LinuxPackets, WindowsPackets
from .passive import Conversation, decode_packet
from .runtime import InstanceLock, atomic_json, read_json, supervisor_gone
from .snapshots import SnapshotStore, migrate_database, native_database_directory


class CaptureEngine:
    def __init__(self, store, discovery, label, capture_bodies=True, flow_limit=64, idle_timeout=1200):
        self.store,self.discovery,self.label=store,discovery,label
        self.capture_bodies,self.flow_limit,self.idle_timeout=capture_bodies,flow_limit,idle_timeout
        self.flows={};self.disabled={};self.interfaces=Counter();self.errors=Counter()
        self.stats={'requests':0,'responses':0,'responses_without_request':0,'packets':0,'parse_errors':0}
        self.last_packet_epoch=None;self.last_request_epoch=None

    def annotate(self, record):
        record['capture_host']=self.label
        self.discovery.attribute(record)

    def process(self, packet, interface, ports):
        parsed=None;key=None
        try:
            for port in ports:
                parsed=decode_packet(packet,port)
                if parsed:
                    break
            if not parsed:return
            peer,direction,seq,flags,payload=parsed;key=(port,peer)
            self.stats['packets']+=1;self.interfaces[interface]+=1;self.last_packet_epoch=time.time()
            syn=direction==0 and bool(flags&2)
            if key in self.disabled:
                if not syn:return
                self.disabled.pop(key,None)
            flow=self.flows.get(key)
            if syn and flow is not None:
                if getattr(flow,'initial_seq',None)==seq:return  # SYN retransmission / duplicate interface copy.
                self.flows.pop(key).close('new_connection');flow=None
            if flow is None:
                if not payload and not flags&2:return
                if len(self.flows)>=self.flow_limit:
                    oldest=min(self.flows,key=lambda k:self.flows[k].last_packet)
                    self.flows.pop(oldest).close('capture_connection_limit')
                flow=Conversation(self.store,peer,self.stats,port,owners=self.discovery.owners.get(peer,[]),
                    source='windows-passive' if os.name=='nt' else 'wsl-passive',capture_bodies=self.capture_bodies,
                    annotate=self.annotate,quiet=True)
                flow.initial_seq=seq if syn else None
                self.flows[key]=flow
            flow.last_packet=time.monotonic()
            if flags&2 and flow.tcp[direction].next is None:
                flow.tcp[direction].next=(seq+1)%2**32
            before=self.stats['requests']
            flow.tcp[direction].feed((seq+1)%2**32 if flags&2 else seq,payload)
            if self.stats['requests']>before:self.last_request_epoch=time.time()
            if flags&4 or (flags&1 and direction==1):
                flow.close('connection_reset' if flags&4 else None);self.flows.pop(key,None)
                self.disabled[key]=time.monotonic()  # Ignore late duplicate payload after FIN/RST.
        except Exception as exc:
            name=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
            # Never put exception text with arbitrary protocol bytes into status files.
            known={'http_header_limit','invalid_header','ambiguous_framing','not_http1_request','not_http1_response',
                   'passive_websocket_not_supported','passive_http2_not_supported','passive_tls_not_supported','invalid_length','chunk_header_limit','invalid_chunk_size',
                   'invalid_chunk_end','trailer_limit','request_body_limit','tcp_reassembly_limit','packet_truncated'}
            name=name if name in known else type(exc).__name__
            self.errors[name]+=1;self.stats['parse_errors']+=1
            if key in self.flows:self.flows.pop(key).close('capture_parse_error')
            if key is not None:self.disabled[key]=time.monotonic()

    def sweep(self, ports):
        now=time.monotonic()
        for key,flow in list(self.flows.items()):
            if key[0] not in ports or now-flow.last_packet>self.idle_timeout:
                flow.close('listener_disappeared' if key[0] not in ports else 'capture_idle_timeout')
                self.flows.pop(key,None)
        self.disabled={k:t for k,t in self.disabled.items() if now-t<120}

    def close(self, reason):
        for flow in self.flows.values():flow.close(reason)
        self.flows.clear();self.disabled.clear()


def worker(args):
    root=Path(args.control_dir);directory=Path(args.data_dir)
    exiting=False
    def stop(*_):
        nonlocal exiting
        exiting=True
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    with InstanceLock(directory/'capture.lock'):
        native=native_database_directory(directory)
        migrate_database(directory,native)
        store=SnapshotStore(directory,args.retention,database_dir=native)
        discover=(WindowsDiscovery if os.name=='nt' else LinuxDiscovery)(store.salt)
        engine=CaptureEngine(store,discover,args.label,capture_bodies=not args.metadata_only)
        source=None;retry_at=0.;last_status=0.;state='starting';error=None;drops=0
        started=time.time();initial_processes=[];control={};next_control=0.;last_supervisor={}
        def publish(final=False):
            nonlocal drops
            if source:
                try:
                    current_drops=source.dropped()
                    if current_drops is not None and current_drops>drops:
                        engine.close('capture_packet_loss')
                    if current_drops is not None:drops=current_drops
                except OSError:pass
            store.publish_snapshot(force=final)
            atomic_json(directory/'capture-status.json',{
                'name':args.label,'pid':os.getpid(),'run_id':args.run_id,'state':state,'error':error,
                'updated_epoch':time.time(),'started_epoch':started,'ports':sorted(discover.ports|set(args.port)),
                'available_interfaces':discover.interfaces,'observed_interfaces':dict(engine.interfaces),
                'stats':engine.stats,'errors':dict(engine.errors),'kernel_drops':drops,
                'last_packet_epoch':engine.last_packet_epoch,'last_request_epoch':engine.last_request_epoch,
                'active_connections':len(engine.flows),'processes':discover.processes,
                'processes_at_start':initial_processes,'boundary':'client_to_ccswitch',
                'capture_bodies':not args.metadata_only,'retention':args.retention,
                'data_transport':'snapshot-v1','database':str(store.database_directory/'captures.db'),
                'snapshot_published_epoch':store.published_epoch,'snapshot_error':store.publication_error})
        try:
            while not exiting:
                if time.monotonic()>=next_control:
                    control=read_json(root/'control.json',default=control);next_control=time.monotonic()+.5
                    supervisor=read_json(root/'watch-status.json')
                    if supervisor:last_supervisor=supervisor
                    if supervisor_gone(last_supervisor,args.run_id):break
                if control.get('stop_run_id')==args.run_id:break
                discover.scan()
                if not initial_processes and discover.processes:initial_processes=list(discover.processes)
                paused=bool(control.get('paused'))
                ports=discover.ports | set(args.port)
                if paused:
                    if source:source.close();source=None
                    engine.close('capture_paused');state='paused'
                elif not ports:
                    if source:source.close();source=None
                    engine.close('listener_disappeared');state='waiting_listener';error=None
                else:
                    if source is None and time.monotonic()<retry_at:
                        state='recovering' if error=='抓取接口断开，正在重新连接' else 'unavailable'
                    if source is None and time.monotonic()>=retry_at:
                        try:
                            source=(WindowsPackets if os.name=='nt' else LinuxPackets)()
                            drops=0
                            error=None
                        except (OSError,CaptureUnavailable) as exc:
                            error=str(exc) if isinstance(exc,CaptureUnavailable) else ('权限不足：旁路抓取需要本机 CAP_NET_RAW/root' if isinstance(exc,PermissionError) else type(exc).__name__)
                            state='unavailable';retry_at=time.monotonic()+30
                    if source:
                        state='capturing' if engine.last_packet_epoch and time.time()-engine.last_packet_epoch<15 else 'waiting_traffic'
                        try:
                            if os.name=='nt':source.set_ports(ports)
                            received=source.receive()
                            if received:engine.process(*received,ports)
                            elif os.name=='nt':time.sleep(.02)
                        except (OSError,CaptureUnavailable):
                            source.close();source=None;engine.close('capture_source_lost')
                            state='recovering';error='抓取接口断开，正在重新连接';retry_at=time.monotonic()+2
                if not source:time.sleep(.2)
                if time.monotonic()-last_status>=2:
                    engine.sweep(ports);publish();last_status=time.monotonic()
        finally:
            engine.close('capture_service_stopped');state='stopped'
            publish(final=True)
            if source:source.close()
            if hasattr(discover,'close'):discover.close()
            store.close()


def main():
    p=argparse.ArgumentParser(description='Route Scope managed receive-only worker')
    p.add_argument('--data-dir',required=True);p.add_argument('--control-dir',required=True)
    p.add_argument('--run-id',required=True);p.add_argument('--label',required=True)
    p.add_argument('--port',action='append',type=int,default=[])
    p.add_argument('--retention',type=int,default=1000);p.add_argument('--metadata-only',action='store_true')
    args=p.parse_args()
    if args.retention<1 or any(not 1<=x<=65535 for x in args.port):p.error('invalid capture limits')
    worker(args)


if __name__=='__main__':main()
