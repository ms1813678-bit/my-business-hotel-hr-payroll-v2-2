"""Portable startup-probe packet controls, never Windows execution or acceptance."""
from pathlib import Path
import hashlib
import importlib.util
import json
import re
import shutil
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[1]
D=ROOT/'verification/windows_startup_probe'

STUB='''#include <stddef.h>
#include <stdint.h>
typedef wchar_t WCHAR; typedef void *HANDLE; typedef void *HMODULE; typedef unsigned long DWORD; typedef long LONG; typedef uintptr_t (*FARPROC)(void);
#define WINAPI
#define __cdecl
typedef unsigned long ULONG;
#define INVALID_HANDLE_VALUE ((HANDLE)(intptr_t)-1)
#define GENERIC_READ 1
#define FILE_SHARE_READ 1
#define OPEN_EXISTING 3
#define FILE_ATTRIBUTE_NORMAL 128
#define ERROR_INVALID_DATA 13
#define ERROR_BAD_PATHNAME 161
#define ERROR_INVALID_HANDLE 6
#define STD_INPUT_HANDLE 1
#define STD_OUTPUT_HANDLE 2
#define STD_ERROR_HANDLE 3
#define FILE_TYPE_UNKNOWN 0
extern int fail_step; extern unsigned fail_err; extern unsigned code_out;
HMODULE LoadLibraryExW(const wchar_t *n, void *f, DWORD fl);
FARPROC GetProcAddress(HMODULE m, const char *n);
DWORD GetLastError(void); void SetLastError(DWORD e);
DWORD GetModuleFileNameW(HMODULE m, wchar_t *b, DWORD n);
HANDLE CreateFileW(const wchar_t *p, DWORD a, DWORD s, void *sa, DWORD d, DWORD f, void *t);
int ReadFile(HANDLE h, void *b, DWORD n, DWORD *g, void *o); int CloseHandle(HANDLE h);
HANDLE GetStdHandle(DWORD n); DWORD GetFileType(HANDLE h);
HANDLE GetCurrentProcess(void); int TerminateProcess(HANDLE h,unsigned c);
'''
HARNESS='''#include "windows.h"
#include <stdio.h>
#include <stdlib.h>
int fail_step=0; unsigned fail_err=0; unsigned code_out=0;
static DWORD last=0; static int calls_load=0;
static const char *version(void){return fail_step==3?"2.7":"3.14.10 x";}
static LONG gen(void *a,unsigned char *b,DWORD n,DWORD f){(void)a;(void)b;(void)n;(void)f;return fail_step==4?(LONG)(int)0xC0000022:0;}
HMODULE LoadLibraryExW(const wchar_t *n,void *f,DWORD fl){(void)f;(void)fl;calls_load++;
 if(n[0]==L'p'){ if(fail_step==1){last=fail_err;return NULL;} return (HMODULE)1;}
 if(fail_step==4 && 0){} return (HMODULE)2;}
FARPROC GetProcAddress(HMODULE m,const char *n){(void)n; if((uintptr_t)m==1){ if(fail_step==2){last=fail_err;return 0;} return (FARPROC)(void*)version;} return (FARPROC)(void*)gen;}
DWORD GetLastError(void){return last;} void SetLastError(DWORD e){last=e;}
DWORD GetModuleFileNameW(HMODULE m,wchar_t *b,DWORD n){(void)m;(void)n;const wchar_t *p=L"C:\\\\stage\\\\runtime\\\\python.exe";DWORD i=0;for(;p[i];i++)b[i]=p[i];b[i]=0;return i;}
static int which(const wchar_t *p){ int i=0; while(p[i])i++; wchar_t c=p[i-1]; return c==L'p'?5:(c==L'h'?6:7);}
HANDLE CreateFileW(const wchar_t *p,DWORD a,DWORD s,void *sa,DWORD d,DWORD f,void *t){(void)a;(void)s;(void)sa;(void)d;(void)f;(void)t; int w=which(p); if(fail_step==w){last=fail_err;return INVALID_HANDLE_VALUE;} return (HANDLE)(intptr_t)w;}
int ReadFile(HANDLE h,void *b,DWORD n,DWORD *g,void *o){(void)h;(void)n;(void)o;*(unsigned char*)b=1;*g=1;return 1;}
int CloseHandle(HANDLE h){(void)h;return 1;}
HANDLE GetStdHandle(DWORD n){ if(fail_step==7+(int)n){return INVALID_HANDLE_VALUE;} return (HANDLE)(intptr_t)(100+n);}
DWORD GetFileType(HANDLE h){(void)h;return 3;}
HANDLE GetCurrentProcess(void){return (HANDLE)7;}
int TerminateProcess(HANDLE h,unsigned c){(void)h;code_out=c;printf("%u\\n",c);exit(0);return 1;}
int main(int argc,char **argv){ if(argc>1)fail_step=atoi(argv[1]); if(argc>2)fail_err=(unsigned)atoi(argv[2]); extern void mainCRTStartup(void); mainCRTStartup(); return 1;}
'''


def load(file):
    spec=importlib.util.spec_from_file_location(file[:-3],D/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def build(tmp_path):
    if not shutil.which('gcc'):pytest.skip('gcc unavailable')
    (tmp_path/'windows.h').write_text(STUB);(tmp_path/'h.c').write_text('#define _GNU_SOURCE\n#include <wchar.h>\n'+HARNESS)
    exe=tmp_path/'probe'
    r=subprocess.run(['gcc','-std=gnu11','-w','-fshort-wchar','-I',str(tmp_path),str(D/'startup_probe.c'),str(tmp_path/'h.c'),'-o',str(exe)],capture_output=True,text=True)
    if r.returncode:pytest.skip('stub build unavailable: '+r.stderr[:200])
    return exe


def run(exe,*args):
    return int(subprocess.run([str(exe),*map(str,args)],capture_output=True,text=True).stdout.strip())


def test_stub_harness_encoding_matches_decoder(tmp_path):
    exe=build(tmp_path);d=load('decode_startup.py')
    rows,first,err=d.decode(run(exe))
    assert first==0 and err==0 and all(r['ok'] for r in rows)
    for step,e in ((1,5),(2,127),(5,2),(6,3),(7,32),(8,0),(9,0),(10,0)):
        rows,first,err=d.decode(run(exe,step,e))
        assert first==step and not rows[step-1]['ok']
        if step in (1,2,5,6,7):assert err==e
    rows,first,err=d.decode(run(exe,4,0))
    assert first==4 and err==0x22 and not rows[3]['ok'] and rows[0]['ok']
    rows,first,err=d.decode(run(exe,3,0))
    assert first==3 and not rows[2]['ok'] and rows[1]['ok']
    for step in (1,2,3,4,5,6,7):assert run(exe,step,5)<0x40000000


def test_decode_refusals_and_range():
    d=load('decode_startup.py')
    for bad in (0xC0000022,77,0x40000000,0x20000000|(11<<10),0x20000000|0x3FF|(1<<10),0x20000000|(1<<10)|1,'5'):
        with pytest.raises(ValueError):d.decode(bad)
    rows,first,err=d.decode(0x20000000|0x3FF)
    assert first==0 and err==0 and len(rows)==10


def test_decoder_prints_interpretation_rules_and_raw_crash_code(tmp_path):
    import subprocess,sys
    ok=subprocess.run([sys.executable,str(D/'decode_startup.py'),'--exit',str(0x20000000|0x3FF)],capture_output=True,text=True)
    assert ok.returncode==0
    for needle in ('the_failing_dependency_is_NOT_named','error_is_NTSTATUS_low15','report_raw_do_not_decode','later_failures_are_not_causal','not_acceptance','python_exe_image_properties'):assert needle in ok.stdout
    bad=subprocess.run([sys.executable,str(D/'decode_startup.py'),'--exit','3221225477'],capture_output=True,text=True)
    assert bad.returncode==2 and 'raw_exit=3221225477' in bad.stdout and 'startup_step' not in bad.stdout


def test_probe_source_kernel32_only_no_python_code_no_unreachable():
    text=(D/'startup_probe.c').read_text()
    code=re.sub(r'/\*.*?\*/','',text,flags=re.S)
    assert re.findall(r'#include\s*[<"]([^>"]+)[>"]',code)==['windows.h']
    assert 'Py_Initialize' not in code and 'PyRun' not in code and 'Py_Main' not in code
    assert code.count('Py_GetVersion')==1 and 'BCryptGenRandom' in code
    assert re.search(r'TerminateProcess\(GetCurrentProcess\(\), 0x20000000u \| mask \| \(first_fail << 10\) \| \(first_err << 14\)\);\s*\}\s*$',code)
    assert 'ExitProcess' not in code.replace('ExitProcess would run','')
    assert 'return' in code  # helper only; entry ends after TerminateProcess
    entry=code[code.index('void mainCRTStartup'):]
    assert 'return' not in entry
    assert 'GENERIC_WRITE' not in code and 'WriteFile' not in code and 'CreateProcess' not in code and 'socket' not in code
    assert 'memset' not in code and 'memcpy' not in code
    assert re.findall(r'L"([^"]+)"',code)==['python314.dll','bcrypt.dll','python314.zip','python314._pth','candidate_worker.py']


def test_pins_workflow_and_unchanged_inputs():
    pins=json.loads((D/'STARTUP-SHA256.json').read_text())
    assert set(pins)=={'verification/windows_startup_probe/startup_probe.c','verification/windows_startup_probe/decode_startup.py'}
    for path,pin in pins.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==pin,path
    for js in ('verification/windows_entry_marker/OBSERVATION-SHA256.json','verification/windows_substitute_exit77/SUBSTITUTE-SHA256.json'):
        for path,pin in json.loads((ROOT/js).read_text()).items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==pin,path
    w=(ROOT/'.github/workflows/windows-startup-probe-observation.yml').read_text()
    assert 'timeout-minutes: 15' in w and 'runs-on: windows-2022' in w and 'matrix:' not in w
    assert 'verification/windows_startup_probe/**' in w and 'windows_loader_probe/**' not in w and 'windows_substitute_exit77/**' not in w
    assert '$sub/stage_substitute.py' in w and 'probe_closure' not in w and 'probe_acl_model' not in w
    assert 'security_acceptance=NOT_ESTABLISHED' in w and 'negative_inconclusive=1' in w and 'diagnostic_only=1' in w
    assert w.index('startup_probe.c')<w.index('stage_substitute.py')<w.index('$safeCleanup=$false')<w.index('& pwsh -NoProfile -File')<w.index('decode_startup.py')<w.index('if(-not $safeCleanup)')<w.index('cleanup --root $owned')
    assert 'if($diagnosticExit -in @(0,122,123,124,126))' in w
    assert 'ALL_APPLICATION' not in w and 'icacls' not in w and 'logman' not in w and 'retry' not in w.lower()
    assert 'Startup probe compile failed' in w and '/GS- /Gy' in w
    assert (ROOT/'verification/windows_entry_marker/qualify_diagnostic.ps1').read_bytes()==(ROOT/'verification/windows_nodebug_diagnostic/qualify_diagnostic.ps1').read_bytes()
