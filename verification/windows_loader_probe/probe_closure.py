"""Compute the probe name list from dumpbin /dependents output. Read-only, no launch.
Pure functions are injectable for portable tests. The real resolver runs only in the
runner's own ordinary process (never in the AppContainer) and is runner resolution, not
a measurement of what the contained child can load.
Limits: static imports only (dumpbin /dependents); System32 libraries are not recursed.
Apiset names collapse deterministically: one sorted-first representative for each of the
api-ms-win-crt-* and api-ms-win-core-* families, then deduplication by resolved host.
Priority if more than MAX_BITS remain: python314.dll, then system names, then other
runtime-dir names, each sorted by name. The unprobed names are returned and printed.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

MAX_BITS=28
NAME=re.compile(r'^[a-z0-9][a-z0-9._-]{0,62}\.(dll|pyd)$')
SKIP={'python.exe'}
FAMILIES=('api-ms-win-crt-','api-ms-win-core-')


def parse_dependents(text):
    names=[];seen=False
    for line in text.splitlines():
        s=line.strip()
        if 'has the following dependencies' in s:seen=True;continue
        if seen and s.lower().endswith('.dll'):
            name=s.lower()
            if not NAME.fullmatch(name):raise ValueError('Unexpected dependency name refused')
            names.append(name)
        elif seen and s.startswith('Summary'):break
    return names


def compute(runtime,run_dumpbin,resolve,known=frozenset()):
    """run_dumpbin(path)->text; resolve(name, runtime_path_or_None)->host path string or ''."""
    runtime=Path(runtime);roots=[runtime/'python.exe',runtime/'python314.dll']
    for root in roots:
        if not root.is_file():raise ValueError('Runtime root file missing')
    items={};order=[];visited=set();queue=list(roots)
    while queue:
        path=queue.pop(0)
        if path.name.lower() in visited:continue
        visited.add(path.name.lower())
        for name in parse_dependents(run_dumpbin(path)):
            if name in SKIP or name in items:continue
            family=next((f for f in FAMILIES if name.startswith(f)),None)
            if family and any(n.startswith(family) for n in items):continue
            local=runtime/name
            local=local if local.is_file() else None
            host=resolve(name,local)
            key=(host or name).lower()
            klass='runtime_dir' if local else 'system_or_unresolved'
            if key in (v['key'] for v in items.values()):continue
            items[name]={'name':name,'host':host,'class':klass,'key':key,'known_dll':int(name in known)}
            order.append(name)
            if local and name not in visited:queue.append(local)
        if len(visited)>64:raise ValueError('Closure file limit exceeded')
    def priority(n):
        return (0 if n=='python314.dll' else 1 if items[n]['class']!='runtime_dir' else 2,n)
    ordered=sorted(order,key=priority)
    if not ordered:raise ValueError('Probe item count out of range: 0')
    probed,unprobed=ordered[:MAX_BITS],ordered[MAX_BITS:]
    rows=[{'bit':i,'name':n,'host':items[n]['host'],'class':items[n]['class'],'known_dll':items[n]['known_dll']} for i,n in enumerate(probed)]
    return rows,unprobed


def header(items):
    if not 1<=len(items)<=MAX_BITS:raise ValueError('Probe item count out of range')
    for item in items:
        if not NAME.fullmatch(item['name']):raise ValueError('Bad probe name')
    lines=['#define PROBE_COUNT '+str(len(items)),'static const wchar_t *const PROBE_NAMES[PROBE_COUNT]={']
    lines+=['    L"'+item['name']+'",' for item in items]
    lines.append('};')
    return '\n'.join(lines)+'\n'


def real_known_dlls():
    import winreg
    names=set()
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,r'SYSTEM\CurrentControlSet\Control\Session Manager\KnownDLLs') as key:
            for i in range(512):
                try:_,value,_=winreg.EnumValue(key,i)
                except OSError:break
                if isinstance(value,str) and NAME.fullmatch(value.lower()):names.add(value.lower())
    except OSError:
        return None
    return frozenset(names)


def real_dumpbin(dumpbin):
    def run(path):
        result=subprocess.run([dumpbin,'/nologo','/dependents',str(path)],capture_output=True,text=True,timeout=60)
        if result.returncode or len(result.stdout)>20000:raise ValueError('dumpbin refused')
        return result.stdout
    return run


def real_resolve(name,local):
    if local:return str(local)
    import ctypes
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.LoadLibraryExW.restype=ctypes.c_void_p;k.LoadLibraryExW.argtypes=[ctypes.c_wchar_p,ctypes.c_void_p,ctypes.c_uint32]
    k.GetModuleFileNameW.argtypes=[ctypes.c_void_p,ctypes.c_wchar_p,ctypes.c_uint32]
    k.FreeLibrary.argtypes=[ctypes.c_void_p]
    # DONT_RESOLVE_DLL_REFERENCES | LOAD_LIBRARY_SEARCH_SYSTEM32 in the runner's own process.
    handle=k.LoadLibraryExW(name,None,0x1|0x800)
    if not handle:return ''
    buf=ctypes.create_unicode_buffer(520);n=k.GetModuleFileNameW(handle,buf,520);k.FreeLibrary(handle)
    return buf.value if 0<n<520 else ''


def main():
    p=argparse.ArgumentParser()
    for n in ('runtime','dumpbin','header-out','map-out'):p.add_argument('--'+n,required=True)
    a=p.parse_args()
    known=real_known_dlls()
    if known is None:print('known_dll_list_unavailable=1 known_dll_flags_not_set=1')
    items,unprobed=compute(a.runtime,real_dumpbin(a.dumpbin),real_resolve,known or frozenset())
    Path(a.header_out).write_text(header(items),encoding='ascii')
    Path(a.map_out).write_text(json.dumps(items))
    for item in items:
        print('probe_item bit=%d name=%s class=%s known_dll=%d host=%s runner_resolution_only=1 known_dll_bits_say_nothing_about_acls=1'%(item['bit'],item['name'],item['class'],item['known_dll'],Path(item['host']).name.lower() if item['host'] else 'unresolved'))
    for name in unprobed:print('probe_unprobed name='+name+' over_bit_budget=1')
    print('probe_count='+str(len(items))+' probe_unprobed='+str(len(unprobed))+' static_imports_only=1 system32_not_recursed=1')


if __name__=='__main__':
    try:main()
    except ValueError as exc:
        print('probe_closure_refused '+str(exc)[:80]);sys.exit(2)
