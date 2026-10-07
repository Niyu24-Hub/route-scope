"""Receive-only packet sources. No driver installation or packet injection."""
import ctypes
import os
from pathlib import Path
import socket
import struct


class LinuxPackets:
    name = 'linux-af-packet'
    def __init__(self):
        self.socket=socket.socket(socket.AF_PACKET,socket.SOCK_RAW,socket.htons(3))
        self.socket.setsockopt(socket.SOL_SOCKET,socket.SO_RCVBUF,8*1024*1024)
        self.socket.settimeout(.2)
        self.drops=0

    def receive(self):
        try:
            data,address=self.socket.recvfrom(131072)
            return data,address[0]
        except socket.timeout:
            return None

    def dropped(self):
        _,delta=struct.unpack('II',self.socket.getsockopt(263,6,8))
        self.drops+=delta
        return self.drops

    def close(self):
        self.socket.close()


class CaptureUnavailable(RuntimeError):
    pass


class WindowsPackets:
    name = 'windows-pcap'
    def __init__(self):
        self.handle=None;self.dll_directory=None;self.ports=None
        system=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'
        library=next((p for p in (system/'Npcap/wpcap.dll',system/'wpcap.dll') if p.is_file()),None)
        if library is None:
            raise CaptureUnavailable('未安装提供回环捕获的 Npcap；Windows 本机流量不可旁路读取')
        self.dll_directory=os.add_dll_directory(str(library.parent)) if hasattr(os,'add_dll_directory') else None
        self.pcap=ctypes.CDLL(str(library))
        class Device(ctypes.Structure): pass
        Device._fields_=[('next',ctypes.POINTER(Device)),('name',ctypes.c_char_p),('description',ctypes.c_char_p),('addresses',ctypes.c_void_p),('flags',ctypes.c_uint)]
        class Timeval(ctypes.Structure): _fields_=[('seconds',ctypes.c_long),('microseconds',ctypes.c_long)]
        class Header(ctypes.Structure): _fields_=[('ts',Timeval),('caplen',ctypes.c_uint),('len',ctypes.c_uint)]
        self.Header=Header;p=self.pcap
        class BPFProgram(ctypes.Structure):
            _fields_=[('length',ctypes.c_uint),('instructions',ctypes.c_void_p)]
        self.BPFProgram=BPFProgram
        p.pcap_compile.argtypes=[ctypes.c_void_p,ctypes.POINTER(BPFProgram),ctypes.c_char_p,ctypes.c_int,ctypes.c_uint]
        p.pcap_compile.restype=ctypes.c_int
        p.pcap_setfilter.argtypes=[ctypes.c_void_p,ctypes.POINTER(BPFProgram)];p.pcap_setfilter.restype=ctypes.c_int
        p.pcap_freecode.argtypes=[ctypes.POINTER(BPFProgram)]
        p.pcap_setbuff.argtypes=[ctypes.c_void_p,ctypes.c_int];p.pcap_setbuff.restype=ctypes.c_int
        p.pcap_findalldevs.argtypes=[ctypes.POINTER(ctypes.POINTER(Device)),ctypes.c_char_p];p.pcap_findalldevs.restype=ctypes.c_int
        p.pcap_freealldevs.argtypes=[ctypes.POINTER(Device)]
        p.pcap_open_live.argtypes=[ctypes.c_char_p,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_char_p];p.pcap_open_live.restype=ctypes.c_void_p
        p.pcap_close.argtypes=[ctypes.c_void_p]
        p.pcap_datalink.argtypes=[ctypes.c_void_p];p.pcap_datalink.restype=ctypes.c_int
        p.pcap_setnonblock.argtypes=[ctypes.c_void_p,ctypes.c_int,ctypes.c_char_p];p.pcap_setnonblock.restype=ctypes.c_int
        p.pcap_next_ex.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.POINTER(Header)),ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))];p.pcap_next_ex.restype=ctypes.c_int
        err=ctypes.create_string_buffer(256);head=ctypes.POINTER(Device)()
        if p.pcap_findalldevs(ctypes.byref(head),err)!=0:
            self.close();raise CaptureUnavailable('Windows 抓包接口枚举失败；可能缺少访问权限')
        found=None
        try:
            node=head
            while node:
                d=node.contents;name=d.name or b'';description=d.description or b''
                if d.flags&1 or b'loopback' in name.lower() or b'loopback' in description.lower():
                    found=bytes(name);break
                node=d.next
        finally:
            if head:p.pcap_freealldevs(head)
        if found is None:
            self.close();raise CaptureUnavailable('现有 WinPcap/Npcap 没有回环接口；Windows 本机覆盖不可用，WSL 抓取不受影响')
        self.interface=found.decode(errors='replace')
        self.handle=p.pcap_open_live(found,131072,0,200,err)
        if not self.handle:
            self.close();raise CaptureUnavailable('无法打开 Windows 回环接口；请检查现有驱动和捕获权限')
        if p.pcap_setbuff(self.handle,8*1024*1024)!=0:
            self.close();raise CaptureUnavailable('无法设置 Windows 抓包缓冲区')
        self.link=p.pcap_datalink(self.handle)
        if self.link not in (0,1,108):
            self.close();raise CaptureUnavailable('Windows 回环链路类型不支持')
        if p.pcap_setnonblock(self.handle,1,err)!=0:
            self.close();raise CaptureUnavailable('无法启用回环接口非阻塞读取')

    def set_ports(self, ports):
        ports=tuple(sorted(set(ports)))
        if ports==self.ports:return
        if not ports or any(type(port) is not int or not 1<=port<=65535 for port in ports):
            raise ValueError('invalid_capture_ports')
        expression='ip and tcp and host 127.0.0.1 and ('+' or '.join(f'port {port}' for port in ports)+')'
        program=self.BPFProgram()
        if self.pcap.pcap_compile(self.handle,ctypes.byref(program),expression.encode('ascii'),1,0xffffffff)!=0:
            raise CaptureUnavailable('无法编译 Windows 抓包端口过滤器')
        try:
            if self.pcap.pcap_setfilter(self.handle,ctypes.byref(program))!=0:
                raise CaptureUnavailable('无法应用 Windows 抓包端口过滤器')
            self.ports=ports
        finally:
            self.pcap.pcap_freecode(ctypes.byref(program))

    def receive(self):
        head=ctypes.POINTER(self.Header)();data=ctypes.POINTER(ctypes.c_ubyte)()
        result=self.pcap.pcap_next_ex(self.handle,ctypes.byref(head),ctypes.byref(data))
        if result==0:return None
        if result<0:raise CaptureUnavailable('Windows 抓包接口已断开')
        packet=ctypes.string_at(data,head.contents.caplen)
        if self.link in (0,108):
            if len(packet)<5 or packet[4]>>4!=4:return None
            packet=b'\0'*12+b'\x08\x00'+packet[4:]
        return packet,self.interface

    def dropped(self):
        class Stats(ctypes.Structure):
            _fields_=[('recv',ctypes.c_uint),('drop',ctypes.c_uint),('ifdrop',ctypes.c_uint),('capt',ctypes.c_uint)]
        stats=Stats();p=self.pcap
        p.pcap_stats.argtypes=[ctypes.c_void_p,ctypes.POINTER(Stats)];p.pcap_stats.restype=ctypes.c_int
        return stats.drop if p.pcap_stats(self.handle,ctypes.byref(stats))==0 else None

    def close(self):
        if self.handle:
            self.pcap.pcap_close(self.handle);self.handle=None
        if self.dll_directory:
            self.dll_directory.close();self.dll_directory=None
