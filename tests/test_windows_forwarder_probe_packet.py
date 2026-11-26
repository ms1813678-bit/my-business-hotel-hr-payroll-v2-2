"""Portable forwarder-probe packet controls with dumpbin text fixtures. Never Windows execution or acceptance."""
from pathlib import Path
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
D=ROOT/'verification/windows_forwarder_probe'


def load(file):
    spec=importlib.util.spec_from_file_location(file[:-3],D/file)
    module=importlib.util.module_from_spec(spec);sys.modules[file[:-3]]=module;spec.loader.exec_module(module);return module


IMPORTS='''Microsoft (R) COFF/PE Dumper

File Type: DLL

  Section contains the following imports:

    KERNEL32.dll
                1800D6000 Import Address Table
                1800E4BC0 Import Name Table
                        0 time date stamp
                        0 Index of first forwarder reference

                          2A6 GetStdHandle
                           7D CreateFileW

    api-ms-win-crt-heap-l1-1-0.dll
                1800D6100 Import Address Table
                        0 time date stamp

                           12 malloc
                Ordinal   44

    VCRUNTIME140.dll
                          1E  memcpy

    ADVAPI32.dll
                           9  RegCloseKey
                           A  CryptGenRandomX
'''
def exp(*lines,names=None):
    n=len(lines) if names is None else names
    return 'Dump of file x.dll\n\n  Section contains the following exports\n\n    00000000 characteristics\n           1 ordinal base\n           %d number of functions\n           %d number of names\n\n    ordinal hint RVA      name\n\n'%(n,n)+'\n'.join(lines)+'\n\n  Summary\n'
EXPORTS={
 'kernel32':exp('        1    0 0001A2B0 GetStdHandle','        2    1          CreateFileW (forwarded to api-ms-win-core-file-l1-1-0.CreateFileW)','        3    2 0001A2C0 Unused'),
 'ucrtbase':exp('        1    0          malloc (forwarded to api-ms-win-crt-private-l1-1-0.malloc)'),
 'advapi32':exp('        1    0          RegCloseKey (forwarded to KERNELBASE.RegCloseKey)','        2    1          CryptGenRandomX (forwarded to cryptsp.CryptGenRandomX)'),
 'version':exp('        1    0 00001000 GetFileVersionInfoW'),
 'ws2_32':exp('        1    0 00001000 WSAStartup'),
 'bcrypt':exp('        1    0 00001000 BCryptGenRandom'),
}
EXPORTS['api-ms-win-core-file-l1-1-0']=None


FIXTURE_PINS={'kernel32':2,'api-ms-win-crt-heap-l1-1-0':1,'vcruntime140':1,'advapi32':2}


def tables(f):
    return {h:f.parse_exports(EXPORTS[h]) for h in f.HOSTS}


def test_parse_imports_case_missing_dll_ordinals_and_skips():
    f=load('forwarders.py');imp=f.parse_imports(IMPORTS)
    assert imp==[('kernel32','GetStdHandle'),('kernel32','CreateFileW'),('api-ms-win-crt-heap-l1-1-0','malloc'),('vcruntime140','memcpy'),('advapi32','RegCloseKey'),('advapi32','CryptGenRandomX')]


def test_parse_exports_forwarded_and_plain_lowercase_host():
    f=load('forwarders.py');t=f.parse_exports(EXPORTS['kernel32'])
    assert t['GetStdHandle'] is None and t['CreateFileW']==('api-ms-win-core-file-l1-1-0','CreateFileW')
    assert f.parse_exports(EXPORTS['advapi32'])['CryptGenRandomX']==('cryptsp','CryptGenRandomX')


def test_targets_drop_resident_and_proven_names_sorted_and_chain_depth():
    f=load('forwarders.py');imp=f.parse_imports(IMPORTS);ex=tables(f)
    names,by,over=f.build_targets(imp,ex)
    # kernelbase dropped, crt private apiset (via ucrtbase) and cryptsp and core-file kept; sorted
    assert names==sorted(names)==['api-ms-win-core-file-l1-1-0.dll','api-ms-win-crt-private-l1-1-0.dll','cryptsp.dll'] and over==[]
    assert by['cryptsp.dll']==['advapi32!CryptGenRandomX'] and by['api-ms-win-crt-private-l1-1-0.dll']==['ucrtbase!malloc']
    for dropped in ('ntdll.dll','kernelbase.dll','kernel32.dll','bcrypt.dll','api-ms-win-crt-heap-l1-1-0.dll','api-ms-win-core-path-l1-1-0.dll','version.dll'):assert dropped not in names
    # deterministic regardless of import order
    assert f.build_targets(list(reversed(imp)),ex)[0]==names


def test_chain_follows_to_depth_three_only():
    f=load('forwarders.py')
    ex2={h:{} for h in f.HOSTS};ex2['kernel32']={'A':('ucrtbase','B')};ex2['ucrtbase']={'B':('advapi32','C')};ex2['advapi32']={'C':('bcrypt','D')}
    ex2['bcrypt']={'D':('cryptsp','E')}
    names,by,over=f.build_targets([('kernel32','A')],ex2)
    # hop1 ucrtbase (host, kept: not in DROP), hop2 advapi32 dropped, hop3 bcrypt dropped, hop4 cryptsp never reached
    assert names==['ucrtbase.dll'] and 'cryptsp.dll' not in by


def test_cap_twenty_with_overflow_reported():
    f=load('forwarders.py')
    ex={h:{} for h in f.HOSTS};ex['kernel32']={'F%d'%i:('zz%02d'%i,'X') for i in range(25)}
    names,by,over=f.build_targets([('kernel32','F%d'%i) for i in range(25)],ex)
    assert len(names)==20 and len(over)==5 and names+over==sorted(names+over)
    listing=f.render_listing(names,by,over)
    assert listing.count('unprobed_overflow')==5 and listing.count('target index=')==20


def test_empty_and_oversize_and_malformed_refused():
    f=load('forwarders.py')
    with pytest.raises(f.Refused):f.render_header([])
    with pytest.raises(f.Refused):f.parse_imports('x'*(f.MAX_TEXT+1))
    with pytest.raises(f.Refused):f.parse_exports('\n'*(f.MAX_LINES+1))
    with pytest.raises(f.Refused):f.parse_exports('x\0')
    with pytest.raises(f.Refused):f.parse_exports('        1    0          A (forwarded to nodot)',require_header=False)
    with pytest.raises(f.Refused):f.parse_exports('        1    0          A (forwarded to bad;host.F)',require_header=False)
    with pytest.raises(f.Refused):f.parse_imports('    bad;name.dll\n')
    ex={h:{} for h in f.HOSTS};ex['kernel32']={'A':('Zz"x','F')}
    with pytest.raises(f.Refused):f.build_targets([('kernel32','A')],ex)
    long='a'*70
    ex['kernel32']={'A':(long,'F')}
    with pytest.raises(f.Refused):f.build_targets([('kernel32','A')],ex)
    # junk lines are ignored, not parsed into targets
    assert f.parse_exports('garbage\n  (forwarded to X.Y)\n',require_header=False)=={}


def test_main_refuses_without_writing_when_list_empty_or_dumpbin_fails(tmp_path,capsys):
    f=load('forwarders.py')
    def runner(db,args):
        if args[0]=='/imports':return IMPORTS
        return exp('        1    0 00001000 Plain')
    code=f.main(['--dumpbin','d','--python-dll','p','--system32',str(tmp_path),'--out-dir',str(tmp_path)],runner=runner,pins=FIXTURE_PINS)
    assert code==2 and 'launch=NOT_ALLOWED' in capsys.readouterr().out and not (tmp_path/'targets.h').exists()
    def boom(db,args):raise f.Refused('dumpbin failed')
    assert f.main(['--dumpbin','d','--python-dll','p','--system32',str(tmp_path),'--out-dir',str(tmp_path)],runner=boom,pins=FIXTURE_PINS)==2
    def good(db,args):
        if args[0]=='/imports':return IMPORTS
        return EXPORTS[Path(args[1]).stem]
    assert f.main(['--dumpbin','d','--python-dll','p','--system32',str(tmp_path),'--out-dir',str(tmp_path)],runner=good,pins=FIXTURE_PINS)==0
    h=(tmp_path/'targets.h').read_text()
    assert '#define TARGET_COUNT 3u' in h and h.count('L"')==3 and 'cryptsp.dll' in h
    listing=(tmp_path/'targets.txt').read_text()
    assert listing.startswith('# chains are followed only through the six dumped hosts') and 'ws2_32 ordinal imports are not covered' in listing
    assert '\ntarget index=1 name=api-ms-win-core-file-l1-1-0.dll forwarded_by=' in listing


STUB='''#include <stddef.h>
#include <stdint.h>
typedef wchar_t WCHAR; typedef void *HANDLE; typedef void *HMODULE; typedef unsigned long DWORD;
#define LOAD_LIBRARY_SEARCH_DEFAULT_DIRS 0x1000
#define LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR 0x100
#define LOAD_WITH_ALTERED_SEARCH_PATH 0x8
HMODULE LoadLibraryExW(const wchar_t *n, void *f, DWORD fl);
int FreeLibrary(HMODULE m);
DWORD GetModuleFileNameW(HMODULE m, wchar_t *b, DWORD n);
HANDLE GetCurrentProcess(void); int TerminateProcess(HANDLE h,unsigned c);
'''
HEADER3='#define TARGET_COUNT 3u\nstatic const wchar_t *const targets[TARGET_COUNT] = {L"aa.dll",L"bb.dll",L"cc.dll"};\n'
HARNESS='''#include "windows.h"
#include <stdio.h>
#include <stdlib.h>
static int fail_bits=0; static int calls=0; static int loaded=0; static int freed=0;
HMODULE LoadLibraryExW(const wchar_t *n,void *f,DWORD fl){ if(f!=NULL){printf("BADARGS\\n");exit(3);}
 int id;
 if(n[0]==L'p') id=100+(int)fl;           /* baseline python314.dll, flags must be 0 */
 else if(n[0]==L'a') id=0; else if(n[0]==L'b') id=1; else if(n[0]==L'c') id=2;
 else id=(fl==0x1100)?20:(fl==0x8?21:99);  /* full path loads */
 if(id==100&&0){}
 calls++; if((fail_bits>>(id==100?22:id))&1) return NULL;
 loaded++; return (HMODULE)1;}
int FreeLibrary(HMODULE m){(void)m;freed++;return 1;}
DWORD GetModuleFileNameW(HMODULE m,wchar_t *b,DWORD n){(void)m;(void)n;const wchar_t *p=L"C:\\\\stage\\\\runtime\\\\python.exe";DWORD i=0;for(;p[i];i++)b[i]=p[i];b[i]=0;return i;}
HANDLE GetCurrentProcess(void){return (HANDLE)7;}
int TerminateProcess(HANDLE h,unsigned c){(void)h;printf("%u %d %d\\n",c,calls,loaded-freed);exit(0);return 1;}
int main(int argc,char **argv){ if(argc>1)fail_bits=atoi(argv[1]); extern void mainCRTStartup(void); mainCRTStartup(); return 1;}
'''


def build(tmp_path):
    if not shutil.which('gcc'):pytest.skip('gcc unavailable')
    (tmp_path/'windows.h').write_text(STUB);(tmp_path/'targets.h').write_text(HEADER3);(tmp_path/'h.c').write_text('#define _GNU_SOURCE\n#include <wchar.h>\n'+HARNESS)
    exe=tmp_path/'probe'
    r=subprocess.run(['gcc','-std=gnu11','-w','-fshort-wchar','-I',str(tmp_path),str(D/'forwarder_probe.c'),str(tmp_path/'h.c'),'-o',str(exe)],capture_output=True,text=True)
    if r.returncode:pytest.skip('stub build unavailable: '+r.stderr[:200])
    return exe


def run(exe,fail=0):
    o=subprocess.run([str(exe),str(fail)],capture_output=True,text=True).stdout.split();return int(o[0]),int(o[1]),int(o[2])


def test_probe_encoding_all_combinations_decode_strictly(tmp_path):
    exe=build(tmp_path);d=load('decode_forwarder.py')
    code,calls,still=run(exe,1<<22)    # only baseline fails? bit means "fail this id"; id100 -> bit 22
    r=d.decode(code,3)
    assert calls==6 and still==0 and r['p0']==0 and r['p1']==[1,1,1] and r['p2']==1 and r['p3']==1 and r['first']==0
    for fail in range(0,1<<6):
        bits=(fail&1)|((fail>>1&1)<<1)|((fail>>2&1)<<2)|((fail>>3&1)<<20)|((fail>>4&1)<<21)|((fail>>5&1)<<22)
        code,calls,still=run(exe,bits);r=d.decode(code,3)
        assert calls==6 and still==0 and code<0x40000000 and not code>>28&1
        assert r['p1']==[1-(fail&1),1-(fail>>1&1),1-(fail>>2&1)] and r['p2']==1-(fail>>3&1) and r['p3']==1-(fail>>4&1) and r['p0']==1-(fail>>5&1)
        want=next((i+1 for i,v in enumerate(r['p1']) if not v),0);assert r['first']==want


def test_decode_refusals_and_reading_rules(tmp_path):
    d=load('decode_forwarder.py')
    for bad in (0xC0000022,77,0x40000000,0x30000000,0x20000000|(1<<8),0x20000000|0x7|(1<<23),0x20000000|0x1|(3<<23),'5'):
        with pytest.raises(ValueError):d.decode(bad,3)
    with pytest.raises(ValueError):d.decode(0x20000007,0)
    with pytest.raises(ValueError):d.decode(0x20000007,21)
    tf=tmp_path/'t.txt';tf.write_text('target index=1 name=aa.dll forwarded_by=kernel32!A\ntarget index=2 name=bb.dll forwarded_by=a!b;c!d\nunprobed_overflow name=zz.dll forwarded_by=x!y\n')
    def go(code):return subprocess.run([sys.executable,str(D/'decode_forwarder.py'),'--exit',str(code),'--targets-file',str(tf)],capture_output=True,text=True)
    cases={0x20000003|(1<<20)|(1<<21):'all_p1_load_and_p2_and_p3_succeed',0x20000003:'all_p1_load_and_p0_p2_p3_all_fail',
           0x20000003|(1<<22):'p0_succeeds',0x20000003|(1<<20):'any_other_combination',0x20000002|(1<<20)|(1<<21)|(1<<23):'p1_some_target_fails'}
    for code,reading in cases.items():
        out=go(code);assert out.returncode==0 and 'forwarder_reading '+reading+'\n' in out.stdout,(hex(code),out.stdout)
        for needle in ('negative_inconclusive=1','security_acceptance=NOT_ESTABLISHED','no_further_instrument_after_this_run','report_raw_do_not_decode','any_other_combination=report_raw_only'):assert needle in out.stdout
    assert 'forwarder_first_fail_target index=1 name=aa.dll' in go(0x20000002|(1<<20)|(1<<21)|(1<<23)).stdout
    bad=go(3221225477);assert bad.returncode==2 and 'raw_exit=3221225477' in bad.stdout and 'forwarder_p1' not in bad.stdout


def test_source_kernel32_only_bounds_and_exit():
    code=re.sub(r'/\*.*?\*/','',(D/'forwarder_probe.c').read_text(),flags=re.S)
    assert re.findall(r'#include\s*[<"]([^>"]+)[>"]',code)==['windows.h','targets.h']
    assert re.findall(r'L"([^"]+)"',code)==['python314.dll']
    assert 'GetProcAddress' not in code and 'Py_' not in code and 'ExitProcess' not in code and 'CreateFile' not in code
    assert code.count('LoadLibraryExW(')==1 and 'LoadLibraryExW(name, NULL, flags)' in code
    assert re.search(r'TerminateProcess\(GetCurrentProcess\(\), 0x20000000u \| mask \| \(first_fail << 23\)\);\s*\}\s*$',code)
    entry=code[code.index('void mainCRTStartup'):]
    assert 'return' not in entry and 'path[dir_len + i]' in entry and 'len < 280' in entry and 'GetModuleFileNameW(NULL, path, 300)' in entry
    assert entry.index('load_free(py_name, 0)')<entry.index('targets[k]')<entry.index('SEARCH_FLAGS_P2')<entry.index('LOAD_WITH_ALTERED_SEARCH_PATH')
    assert 'FreeLibrary(h)' in code


def test_pins_workflow_and_guardrails():
    pins=json.loads((D/'FORWARDER-SHA256.json').read_text())
    assert set(pins)=={'verification/windows_forwarder_probe/'+n for n in ('forwarder_probe.c','decode_forwarder.py','forwarders.py')}
    for path,pin in pins.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==pin,path
    for js in ('verification/windows_entry_marker/OBSERVATION-SHA256.json','verification/windows_substitute_exit77/SUBSTITUTE-SHA256.json'):
        for path,pin in json.loads((ROOT/js).read_text()).items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==pin,path
    w=(ROOT/'.github/workflows/windows-forwarder-probe-observation.yml').read_text()
    assert 'timeout-minutes: 15' in w and 'runs-on: windows-2022' in w and 'matrix:' not in w
    assert 'verification/windows_forwarder_probe/**' in w and 'windows_dependency_probe/**' not in w and w.count('push:')==1
    assert 'security_acceptance=NOT_ESTABLISHED' in w and 'negative_inconclusive=1' in w and 'diagnostic_only=1' in w
    assert w.index('forwarders.py')<w.index("throw 'Target list refused or empty before launch'")<w.index('acl_model')<w.index('forwarder_probe.c')<w.index('stage_substitute.py')<w.index('$safeCleanup=$false')<w.index('& pwsh -NoProfile -File')<w.index('decode_forwarder.py')<w.index('if(-not $safeCleanup)')<w.index('cleanup --root $owned')
    assert 'if($diagnosticExit -in @(0,122,123,124,126))' in w and '"/I$gen"' in w and "'--targets-file'" not in w
    assert 'model_only=1' in w and 'S-1-15-2-2' in w
    for banned in ('icacls','logman','Set-Acl','SetAccessControl','gflags','ALL_APPLICATION','retry','Copy-Item','copy '):assert banned not in w.replace('-ErrorAction','') if banned!='retry' else banned not in w.lower()
    assert 'Forwarder probe compile failed' in w and '/GS- /Gy' in w and 'dependency_probe' not in w and 'decode_dependency' not in w
    assert re.search(r"New-Item -ItemType Directory -Path \$gen",w) and 'HKLM' not in w and 'reg ' not in w


def full_imports(drop=None,extra=None):
    """Synthetic import text matching the pinned per-DLL counts exactly, optionally with one line dropped or an extra DLL."""
    f=load('forwarders.py');out=['  Section contains the following imports:','']
    for stem,n in f.IMPORT_PINS.items():
        out.append('    '+stem.upper()+'.dll' if not stem.startswith('api-ms') else '    '+stem+'.dll')
        out+=['                1800D6000 Import Address Table','                        0 time date stamp','']
        for i in range(n):out.append('                          %X  Func%d'%(i+1,i))
        if stem=='ws2_32':out+=['                      Ordinal   111','                      Ordinal   7','                      Ordinal   19']
        out.append('')
    if extra:out+=['    EXTRA.dll','                          1  Foo','']
    text='\n'.join(out)
    if drop:
        lines=text.split('\n');idx=[i for i,l in enumerate(lines) if l.strip().endswith(drop)][0];del lines[idx];text='\n'.join(lines)
    return text


def test_pinned_import_counts_exact_match_and_refusal_on_any_difference():
    f=load('forwarders.py')
    assert sum(v for k,v in f.IMPORT_PINS.items() if k.startswith('api-ms-win-crt'))==173 and f.IMPORT_PINS['kernel32']==186 and f.IMPORT_PINS['ws2_32']==0
    imp,headers=f.parse_imports_full(full_imports());f.check_import_counts(imp,headers)
    assert len(imp)==sum(f.IMPORT_PINS.values()) and 'ws2_32' in headers
    for text in (full_imports(drop='Func5'),full_imports(extra=True)):
        imp,headers=f.parse_imports_full(text)
        with pytest.raises(f.Refused):f.check_import_counts(imp,headers)
    # a dropped DLL header section (all of one DLL missing) also refuses
    imp,headers=f.parse_imports_full(full_imports().replace('    VERSION.dll','    '))
    with pytest.raises(f.Refused):f.check_import_counts(imp,headers)


def test_main_refuses_on_dropped_import_line_with_real_pins(tmp_path,capsys):
    f=load('forwarders.py')
    def runner(db,args):
        if args[0]=='/imports':return full_imports(drop='Func5')
        return exp('        1    0 00001000 Plain')
    code=f.main(['--dumpbin','d','--python-dll','p','--system32',str(tmp_path),'--out-dir',str(tmp_path)],runner=runner)
    assert code==2 and 'import counts differ' in capsys.readouterr().out and not (tmp_path/'targets.h').exists()


def test_export_header_guard_refuses_dropped_lines():
    f=load('forwarders.py')
    full=exp('        1    0 0001A2B0 A','        2    1 0001A2C0 B','        3    2 0001A2C0 C')
    assert set(f.parse_exports(full))=={'A','B','C'}
    dropped=full.replace('        2    1 0001A2C0 B\n','')
    with pytest.raises(f.Refused):f.parse_exports(dropped)
    with pytest.raises(f.Refused):f.parse_exports(exp('        1    0 0001A2B0 A',names=5))
    with pytest.raises(f.Refused):f.parse_exports('        1    0 0001A2B0 A\n')   # header missing
    two=full.replace('number of names','number of names\n           3 number of names')
    with pytest.raises(f.Refused):f.parse_exports(two)


def test_probe_defines_search_flags_under_ifndef():
    c=(D/'forwarder_probe.c').read_text()
    assert '#ifndef LOAD_LIBRARY_SEARCH_DEFAULT_DIRS\n#define LOAD_LIBRARY_SEARCH_DEFAULT_DIRS 0x1000\n#endif' in c
    assert '#ifndef LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR\n#define LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR 0x100\n#endif' in c
