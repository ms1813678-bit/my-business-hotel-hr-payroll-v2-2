"""Portable substitute-exit77 packet controls, never Windows execution or acceptance."""
from pathlib import Path
import importlib.util
import hashlib
import json
import re
import shutil
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
D=ROOT/'verification/windows_substitute_exit77'
S=ROOT/'verification/windows_integrated_candidate'
M=ROOT/'verification/windows_entry_marker'
SUB=b'MZ-synthetic-substitute'
PIN=hashlib.sha256(SUB).hexdigest()


def load():
    spec=importlib.util.spec_from_file_location('stage_substitute',D/'stage_substitute.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def setup(tmp_path):
    source=tmp_path/'original';source.mkdir()
    for name in load().SOURCE_PINS:shutil.copyfile(S/name,source/name)
    owned=tmp_path/'owned';owned.mkdir()
    (owned/'ENTRY77-OWNER.json').write_text(json.dumps({'owner':'s-1','state':'preparing-entry77'}))
    runtime=owned/'acquired/runtime';runtime.mkdir(parents=True)
    for name in ('python.exe','python314.dll','python314.zip'):(runtime/name).write_bytes(b'original-'+name.encode())
    (runtime/'sub').mkdir();(runtime/'sub/x.pyd').write_bytes(b'x')
    (tmp_path/'sub.exe').write_bytes(SUB)
    return source,owned,runtime,tmp_path/'sub.exe'


def test_derive_replaces_only_python_exe_and_leaves_acquired_untouched(tmp_path):
    m=load();source,owned,runtime,sub=setup(tmp_path)
    before={p.relative_to(runtime).as_posix():p.read_bytes() for p in runtime.rglob('*') if p.is_file()}
    derived,data=m.derive(runtime,owned,'s-1',sub,PIN)
    assert data==SUB and derived==owned/'derived-runtime'
    after={p.relative_to(derived).as_posix():p.read_bytes() for p in derived.rglob('*') if p.is_file()}
    assert after.pop('python.exe')==SUB and before.pop('python.exe')==b'original-python.exe'
    assert after==before
    assert (runtime/'python.exe').read_bytes()==b'original-python.exe'


@pytest.mark.parametrize('mode',['bad-pin','not-mz','oversize','owner','state','occupied','missing-python'])
def test_derive_refuses_adversarial_inputs(tmp_path,mode):
    m=load();source,owned,runtime,sub=setup(tmp_path);pin=PIN
    if mode=='bad-pin':pin='0'*64
    if mode=='not-mz':sub.write_bytes(b'NO');pin=hashlib.sha256(b'NO').hexdigest()
    if mode=='oversize':sub.write_bytes(b'MZ'+b'0'*20000);pin=hashlib.sha256(sub.read_bytes()).hexdigest()
    if mode=='owner':(owned/'ENTRY77-OWNER.json').write_text('{"owner":"x","state":"preparing-entry77"}')
    if mode=='state':(owned/'ENTRY77-OWNER.json').write_text('{"owner":"s-1","state":"accepted"}')
    if mode=='occupied':(owned/'derived-runtime').mkdir()
    if mode=='missing-python':(runtime/'python.exe').unlink()
    original={p.name:p.read_bytes() for p in runtime.iterdir() if p.is_file()}
    with pytest.raises((ValueError,OSError)):m.derive(runtime,owned,'s-1',sub,pin)
    assert {p.name:p.read_bytes() for p in runtime.iterdir() if p.is_file()}==original


def test_actual_canonical_stager_builds_manifest_with_substitute_and_original_worker(tmp_path):
    source,owned,runtime,sub=setup(tmp_path)
    product=tmp_path/'product';(product/'app').mkdir(parents=True)
    for name in ('fingerprint_evidence.py','attendance_import.py'):(product/'app'/name).write_text('# synthetic')
    packages={}
    for name in ('openpyxl','xlrd','et_xmlfile'):
        q=tmp_path/'packages'/name;q.mkdir(parents=True);(q/'__init__.py').write_text('');packages[name]=str(q)
    (tmp_path/'packages.json').write_text(json.dumps(packages))
    result=subprocess.run([sys.executable,'-B',str(D/'stage_substitute.py'),'--source',str(source),'--owned',str(owned),'--owner','s-1','--runtime',str(runtime),'--substitute',str(sub),'--substitute-pin',PIN,'--product',str(product),'--packages-json',str(tmp_path/'packages.json'),'--destination',str(owned/'staged')],capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    pin=result.stdout.strip();assert re.fullmatch('[0-9a-f]{64}',pin)
    staged=owned/'staged'
    assert (staged/'runtime/python.exe').read_bytes()==SUB
    assert (staged/'runtime/candidate_worker.py').read_bytes()==(S/'candidate_worker.py').read_bytes()
    assert hashlib.sha256((staged/'MANIFEST.json').read_bytes()).hexdigest()==pin
    assert (runtime/'python.exe').read_bytes()==b'original-python.exe'
    assert not (source/'__pycache__').exists()


def test_changed_original_source_refused_before_any_derivation(tmp_path):
    source,owned,runtime,sub=setup(tmp_path)
    (source/'candidate_worker.py').write_text('changed')
    result=subprocess.run([sys.executable,'-B',str(D/'stage_substitute.py'),'--source',str(source),'--owned',str(owned),'--owner','s-1','--runtime',str(runtime),'--substitute',str(sub),'--substitute-pin',PIN,'--product',str(tmp_path),'--packages-json',str(tmp_path),'--destination',str(owned/'staged')],capture_output=True,text=True)
    assert result.returncode!=0 and not (owned/'derived-runtime').exists()


def test_substitute_source_is_kernel32_exit77_only():
    text=(D/'exit77.c').read_text()
    code=re.sub(r'/\*.*?\*/','',text,flags=re.S)
    assert re.findall(r'#include\s*<([^>]+)>',code)==['windows.h']
    assert 'ExitProcess(77)' in code
    for banned in ('CreateFile','LoadLibrary','GetProcAddress','Reg','socket','CreateProcess','fopen','malloc','system','Winhttp','Python'):
        assert banned not in code


def test_pins_workflow_and_unchanged_security_sources():
    pins=json.loads((D/'SUBSTITUTE-SHA256.json').read_text())
    assert set(pins)=={'verification/windows_substitute_exit77/exit77.c','verification/windows_substitute_exit77/stage_substitute.py'}
    for path,pin in pins.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==pin,path
    for path,pin in json.loads((M/'OBSERVATION-SHA256.json').read_text()).items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==pin,path
    w=(ROOT/'.github/workflows/windows-substitute-exit77-observation.yml').read_text()
    assert 'timeout-minutes: 15' in w and 'runs-on: windows-2022' in w and 'matrix:' not in w
    assert 'verification/windows_substitute_exit77/**' in w and 'windows_entry_marker/**' not in w
    assert 'original_python_signature_checks_do_not_cover_substitute=1' in w
    assert '$nativeDiag/launcher_nodebug_diag.c' in w and '$nativeDiag/outer_watchdog.c' in w
    assert '$sub/stage_substitute.py' in w and 'stage_marker.py' not in w
    assert w.index('$safeCleanup=$false')<w.index('& pwsh -NoProfile -File')<w.index('if(-not $safeCleanup)')<w.index('cleanup --root $owned')
    assert 'if($diagnosticExit -in @(0,122,123,124,126))' in w
    assert 'loader_flag.ps1' not in w and 'logman' not in w and 'retry' not in w.lower()
    assert 'python314.dll' in w and "Get-AuthenticodeSignature" in w
    assert (M/'qualify_diagnostic.ps1').read_bytes()==(ROOT/'verification/windows_nodebug_diagnostic/qualify_diagnostic.ps1').read_bytes()
