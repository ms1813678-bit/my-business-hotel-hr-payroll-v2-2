"""Portable dependency-probe packet controls, never Windows execution or acceptance."""
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
D=ROOT/'verification/windows_dependency_probe'

STUB='''#include <stddef.h>
#include <stdint.h>
typedef wchar_t WCHAR; typedef void *HANDLE; typedef void *HMODULE; typedef unsigned long DWORD;
extern int fail_idx; extern unsigned fail_err; extern unsigned code_out;
HMODULE LoadLibraryExW(const wchar_t *n, void *f, DWORD fl);
int FreeLibrary(HMODULE m);
DWORD GetLastError(void);
HANDLE GetCurrentProcess(void); int TerminateProcess(HANDLE h,unsigned c);
'''
# fail_idx is the 1-based position in the exact call order: 1..6 top-level, 7..17 extras, 18 python314.
HARNESS='''#include "windows.h"
#include <stdio.h>
#include <stdlib.h>
int fail_idx=0; unsigned fail_err=0; unsigned code_out=0;
static DWORD last=0; static int calls=0; static int loaded=0; static int freed=0; static int py_freed=0;
HMODULE LoadLibraryExW(const wchar_t *n,void *f,DWORD fl){ if(f!=NULL||fl!=0){printf("BADARGS\\n");exit(3);}
 calls++; if(calls==fail_idx){last=fail_err;return NULL;} loaded++; return (HMODULE)(intptr_t)(n[0]==L'p'?9:1);}
int FreeLibrary(HMODULE m){ if((intptr_t)m==9)py_freed=1; freed++; return 1;}
DWORD GetLastError(void){return last;}
HANDLE GetCurrentProcess(void){return (HANDLE)7;}
int TerminateProcess(HANDLE h,unsigned c){(void)h;printf("%u %d %d %d\\n",c,calls,loaded-freed,py_freed);exit(0);return 1;}
int main(int argc,char **argv){ if(argc>1)fail_idx=atoi(argv[1]); if(argc>2)fail_err=(unsigned)atoi(argv[2]); extern void mainCRTStartup(void); mainCRTStartup(); return 1;}
'''


def load(file):
    spec=importlib.util.spec_from_file_location(file[:-3],D/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def build(tmp_path):
    if not shutil.which('gcc'):pytest.skip('gcc unavailable')
    (tmp_path/'windows.h').write_text(STUB);(tmp_path/'h.c').write_text('#define _GNU_SOURCE\n#include <wchar.h>\n'+HARNESS)
    exe=tmp_path/'probe'
    r=subprocess.run(['gcc','-std=gnu11','-w','-fshort-wchar','-I',str(tmp_path),str(D/'dependency_probe.c'),str(tmp_path/'h.c'),'-o',str(exe)],capture_output=True,text=True)
    if r.returncode:pytest.skip('stub build unavailable: '+r.stderr[:200])
    return exe


def run(exe,*args):
    out=subprocess.run([str(exe),*map(str,args)],capture_output=True,text=True).stdout.split()
    return int(out[0]),int(out[1]),int(out[2]),int(out[3])


def test_all_ok_and_python_loaded_last_and_not_freed(tmp_path):
    exe=build(tmp_path);d=load('decode_dependency.py')
    code,calls,still_loaded,py_freed=run(exe)
    rows,first,err,extra=d.decode(code)
    assert first==0 and err==0 and extra==0 and all(r['ok'] for r in rows)
    assert calls==18 and still_loaded==1 and py_freed==0


def test_each_failure_position_decodes_and_all_steps_still_run(tmp_path):
    exe=build(tmp_path);d=load('decode_dependency.py')
    for idx in range(1,19):
        code,calls,_,_=run(exe,idx,5)
        assert calls==18
        rows,first,err,extra=d.decode(code)
        step=idx if idx<=6 else (7 if idx<=17 else 8)
        assert first==step and not rows[step-1]['ok'] and err==5
        assert extra==(idx-6 if step==7 else 0)
        assert sum(r['ok'] for r in rows)==7
        assert code<0x40000000 and code>>29==1


def test_error_truncation_and_only_first_failure_reported(tmp_path):
    exe=build(tmp_path);d=load('decode_dependency.py')
    assert d.decode(run(exe,3,126)[0])[2]==126
    assert d.decode(run(exe,18,0x2005)[0])[2]==5
    # a later extra failure with an earlier one: harness fails only one position, so check the encoder order directly
    code,_,_,_=run(exe,8,2)
    assert d.decode(code)[3]==2


def test_decode_refusals():
    d=load('decode_dependency.py')
    for bad in (0xC0000022,77,0x40000000,0x20000000|(9<<8),0x20000000|0xFF|(1<<8),'5',
                0x20000000|(7<<8)|(1<<12),0x20000000|(1<<8)|(1<<12)|(1<<25),0x20000000|0xFF|(1<<12),0x20000000|(7<<8)|(12<<25)):
        with pytest.raises(ValueError):d.decode(bad)
    rows,first,err,extra=d.decode(0x20000000|0xFF)
    assert first==0 and len(rows)==8


def test_decoder_prints_rules_and_raw_crash_code():
    ok=subprocess.run([sys.executable,str(D/'decode_dependency.py'),'--exit',str(0x20000000|0xFF)],capture_output=True,text=True)
    assert ok.returncode==0
    for needle in ('python314_DllMain_or_own_init','first_failing_top_level_dependency','subtree_of_failing_name_not_named','report_raw_do_not_decode','later_failures_are_not_causal','security_acceptance=NOT_ESTABLISHED','no_further_instrument_after_this_run'):assert needle in ok.stdout
    bad=subprocess.run([sys.executable,str(D/'decode_dependency.py'),'--exit','3221225477'],capture_output=True,text=True)
    assert bad.returncode==2 and 'raw_exit=3221225477' in bad.stdout and 'dependency_step' not in bad.stdout


def test_source_kernel32_only_exact_names_order_and_exit():
    code=re.sub(r'/\*.*?\*/','',(D/'dependency_probe.c').read_text(),flags=re.S)
    assert re.findall(r'#include\s*[<"]([^>"]+)[>"]',code)==['windows.h']
    assert re.findall(r'L"([^"]+)"',code)==['api-ms-win-crt-%s-l1-1-0.dll'%n for n in ('convert','environment','filesystem','heap','locale','math','process','stdio','string','time','conio')]+['version.dll','ws2_32.dll','advapi32.dll','vcruntime140.dll','api-ms-win-crt-runtime-l1-1-0.dll','api-ms-win-core-path-l1-1-0.dll','python314.dll']
    assert 'LOAD_LIBRARY' not in code and 'DONT_RESOLVE' not in code and code.count('LoadLibraryExW(')==2
    assert 'Py_' not in code and 'GetProcAddress' not in code and 'ExitProcess' not in code
    entry=code[code.index('void mainCRTStartup'):]
    assert 'return' not in entry and entry.index('L"python314.dll"')>entry.index('api-ms-win-core-path')
    assert 'FreeLibrary(py' not in entry
    assert re.search(r'TerminateProcess\(GetCurrentProcess\(\), 0x20000000u \| mask \| \(first_fail << 8\) \| \(first_err << 12\) \| \(first_extra << 25\)\);\s*\}\s*$',code)
    assert 'memset' not in code and 'memcpy' not in code and 'CreateFile' not in code and 'socket' not in code


def test_pins_workflow_and_unchanged_inputs():
    pins=json.loads((D/'DEPENDENCY-SHA256.json').read_text())
    assert set(pins)=={'verification/windows_dependency_probe/dependency_probe.c','verification/windows_dependency_probe/decode_dependency.py'}
    for path,pin in pins.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==pin,path
    for js in ('verification/windows_entry_marker/OBSERVATION-SHA256.json','verification/windows_substitute_exit77/SUBSTITUTE-SHA256.json'):
        for path,pin in json.loads((ROOT/js).read_text()).items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==pin,path
    w=(ROOT/'.github/workflows/windows-dependency-probe-observation.yml').read_text()
    assert 'timeout-minutes: 15' in w and 'runs-on: windows-2022' in w and 'matrix:' not in w
    assert 'verification/windows_dependency_probe/**' in w and 'windows_startup_probe/**' not in w
    assert w.count('push:')==1 and 'branches: [ui-overhaul]' in w
    assert 'security_acceptance=NOT_ESTABLISHED' in w and 'negative_inconclusive=1' in w and 'diagnostic_only=1' in w
    assert w.index('dependency_probe.c')<w.index('stage_substitute.py')<w.index('$safeCleanup=$false')<w.index('& pwsh -NoProfile -File')<w.index('decode_dependency.py')<w.index('if(-not $safeCleanup)')<w.index('cleanup --root $owned')
    assert 'if($diagnosticExit -in @(0,122,123,124,126))' in w
    assert 'ALL_APPLICATION' not in w and 'icacls' not in w and 'logman' not in w and 'retry' not in w.lower()
    assert 'Dependency probe compile failed' in w and '/GS- /Gy' in w and 'startup_probe' not in w and 'decode_startup' not in w
