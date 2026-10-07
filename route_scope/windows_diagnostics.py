"""Read-only capture capability inventory; no driver installation or removal."""
import ctypes
import os
from pathlib import Path


def inventory(open_interface=False):
    if os.name!='nt':return {'platform':os.name,'note':'Windows 驱动诊断需要在 Windows 运行'}
    system=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32'
    result={'npcap_dll':str(system/'Npcap/wpcap.dll'),'npcap_present':(system/'Npcap/wpcap.dll').is_file(),
            'legacy_dll_present':(system/'wpcap.dll').is_file(),'pktmon_present':(system/'pktmon.exe').is_file(),
            'loopback_interfaces':[],'driver_modified':False,'interface_open_verified':False}
    library=next((p for p in (system/'Npcap/wpcap.dll',system/'wpcap.dll') if p.is_file()),None)
    if library:
        directory=os.add_dll_directory(str(library.parent))
        try:
            dll=ctypes.CDLL(str(library))
            dll.pcap_lib_version.restype=ctypes.c_char_p
            result['loaded_library']=str(library);result['version']=dll.pcap_lib_version().decode(errors='replace')
            class Device(ctypes.Structure):pass
            Device._fields_=[('next',ctypes.POINTER(Device)),('name',ctypes.c_char_p),('description',ctypes.c_char_p),('addresses',ctypes.c_void_p),('flags',ctypes.c_uint)]
            dll.pcap_findalldevs.argtypes=[ctypes.POINTER(ctypes.POINTER(Device)),ctypes.c_char_p];dll.pcap_findalldevs.restype=ctypes.c_int
            dll.pcap_freealldevs.argtypes=[ctypes.POINTER(Device)]
            head=ctypes.POINTER(Device)();error=ctypes.create_string_buffer(256)
            if dll.pcap_findalldevs(ctypes.byref(head),error)==0:
                node=head
                try:
                    while node:
                        d=node.contents;name=(d.name or b'').decode(errors='replace');description=(d.description or b'').decode(errors='replace')
                        if d.flags&1 or 'loopback' in (name+' '+description).lower():
                            result['loopback_interfaces'].append({'name':name,'description':description})
                        node=d.next
                finally:
                    if head:dll.pcap_freealldevs(head)
        except OSError as exc:result['error']=type(exc).__name__
        finally:directory.close()
    if open_interface and result['loopback_interfaces']:
        from .packet_sources import WindowsPackets,CaptureUnavailable
        try:
            source=WindowsPackets();result['interface_open_verified']=True;result['link_type']=source.link;source.close()
        except (OSError,CaptureUnavailable) as exc:result['open_error']=str(exc)
    result['recommendation']='Npcap 非 WinPcap 兼容模式共存安装；安装后只重启 Route Scope，再运行 capture-doctor --open-interface。也可用显式 HTTP 网关完全绕过抓包驱动。'
    return result
