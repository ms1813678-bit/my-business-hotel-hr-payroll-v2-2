"""Portable loader-probe packet controls, never Windows execution or acceptance."""
from pathlib import Path
import hashlib
import importlib.util
import json
import re

import pytest

ROOT=Path(__file__).resolve().parents[1]
D=ROOT/'verification/windows_loader_probe'

SAMPLE={
 'python.exe':'Image has the following dependencies:\n\n    python314.dll\n    api-ms-win-crt-runtime-l1-1-0.dll\n    KERNEL32.dll\n\n  Summary\n',
 'python314.dll':'Image has the following dependencies:\n\n    VCRUNTIME140.dll\n    api-ms-win-crt-runtime-l1-1-0.dll\n    api-ms-win-crt-stdio-l1-1-0.dll\n    ADVAPI32.dll\n    KERNEL32.dll\n\n  Summary\n',
 'vcruntime140.dll':'Image has the following dependencies:\n\n    KERNEL32.dll\n    api-ms-win-crt-runtime-l1-1-0.dll\n\n  Summary\n',
}


def load(file):
    spec=importlib.util.spec_from_file_location(file[:-3],D/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def runtime(tmp_path):
    r=tmp_path/'runtime';r.mkdir()
    for n in ('python.exe','python314.dll','vcruntime140.dll'):(r/n).write_bytes(b'x')
    return r


def fake_dump(path):return SAMPLE[path.name.lower()]


def fake_resolve(name,local):
    if local:return str(local)
    hosts={'api-ms-win-crt-runtime-l1-1-0.dll':r'C:\Windows\System32\ucrtbase.dll','api-ms-win-crt-stdio-l1-1-0.dll':r'C:\Windows\System32\ucrtbase.dll'}
    return hosts.get(name,r'C:\Windows\System32\\'+name)


def test_parse_dependents_lowercases_and_stops_at_summary():
    m=load('probe_closure.py')
    assert m.parse_dependents(SAMPLE['python.exe'])==['python314.dll','api-ms-win-crt-runtime-l1-1-0.dll','kernel32.dll']


@pytest.mark.parametrize('bad',['../evil.dll','a b.dll','x'*70+'.dll','evil.exe'])
def test_parse_refuses_unexpected_names(bad):
    m=load('probe_closure.py')
    if not bad.endswith('.dll'):assert m.parse_dependents('Image has the following dependencies:\n  '+bad+'\n')==[]
    else:
        with pytest.raises(ValueError):m.parse_dependents('Image has the following dependencies:\n  '+bad+'\n')


def test_compute_dedupes_by_resolved_host_orders_runtime_first_and_includes_python_dll(tmp_path):
    m=load('probe_closure.py')
    items,unprobed=m.compute(runtime(tmp_path),fake_dump,fake_resolve,frozenset({'kernel32.dll'}))
    names=[i['name'] for i in items]
    assert names[0]=='python314.dll' and names[-1]=='vcruntime140.dll' and 'python.exe' not in names and unprobed==[]
    assert [i['known_dll'] for i in items if i['name']=='kernel32.dll']==[1]
    # two apiset names share one host: exactly one representative.
    assert sum(1 for n in names if n.startswith('api-ms-win-crt'))==1
    assert [i['bit'] for i in items]==list(range(len(items)))
    assert {'advapi32.dll','kernel32.dll'}<=set(names)


def test_compute_collapses_families_and_prioritizes_over_budget(tmp_path):
    m=load('probe_closure.py');r=runtime(tmp_path)
    many='Image has the following dependencies:\n  python314.dll\n'+''.join('  sys%02d.dll\n'%i for i in range(40))+'  api-ms-win-crt-a-l1-1-0.dll\n  api-ms-win-crt-b-l1-1-0.dll\n  api-ms-win-core-a-l1-1-0.dll\n  api-ms-win-core-b-l1-1-0.dll\n'
    dump=lambda p:many if p.name=='python.exe' else SAMPLE['python314.dll'] if p.name=='python314.dll' else SAMPLE['vcruntime140.dll']
    items,unprobed=m.compute(r,dump,lambda n,l:str(l) if l else r'C:\w\\'+n)
    names=[i['name'] for i in items]
    assert len(items)==28 and names[0]=='python314.dll' and len(unprobed)>0
    assert sum(1 for n in names+unprobed if n.startswith('api-ms-win-crt-'))==1 and sum(1 for n in names+unprobed if n.startswith('api-ms-win-core-'))==1
    assert 'vcruntime140.dll' in unprobed  # runtime-dir names have the lowest priority
    assert names==sorted(names[1:]) or names[1:]==sorted(names[1:])


@pytest.mark.parametrize('mode',['missing-root','empty'])
def test_compute_bounds(tmp_path,mode):
    m=load('probe_closure.py');r=runtime(tmp_path)
    if mode=='missing-root':
        (r/'python314.dll').unlink()
        with pytest.raises(ValueError):m.compute(r,fake_dump,fake_resolve)
    else:
        empty='Image has the following dependencies:\n\n  Summary\n'
        with pytest.raises(ValueError):m.compute(r,lambda p:empty,fake_resolve)


def test_header_exact_shape_and_limits():
    m=load('probe_closure.py')
    items=[{'bit':i,'name':'a%d.dll'%i} for i in range(3)]
    h=m.header(items)
    assert h.startswith('#define PROBE_COUNT 3\n') and h.count('L"')==3
    with pytest.raises(ValueError):m.header([])
    with pytest.raises(ValueError):m.header([{'bit':i,'name':'a%d.dll'%i} for i in range(29)])
    with pytest.raises(ValueError):m.header([{'bit':0,'name':'x";system("c.dll'}])


def test_decode_bitmask_roundtrip_and_refusals():
    d=load('decode_probe.py')
    items=[{'bit':i,'name':'n%d.dll'%i} for i in range(4)]
    rows=d.decode(items,0x20000000|0b0101)
    assert [r['loaded'] for r in rows]==[1,0,1,0] and rows[0]['class']=='unknown' and rows[0]['known_dll']==0
    for bad in (0xC0000022,77,0x20000000|0b10000,0x30000000,'5'):
        with pytest.raises(ValueError):d.decode(items,bad)
    for count in (1,28):
        assert 0x20000000|((1<<count)-1)<0x40000000


def test_probe_source_kernel32_only_and_no_unreachable_code():
    text=(D/'probe_template.c').read_text()
    code=re.sub(r'/\*.*?\*/','',text,flags=re.S)
    assert re.findall(r'#include\s*[<"]([^>"]+)[>"]',code)==['windows.h','probe_list.h']
    assert code.count('LoadLibraryExW')==1 and 'DONT_RESOLVE_DLL_REFERENCES' in code and 'FreeLibrary' in code
    assert re.search(r'ExitProcess\(0x20000000u \| mask\);\s*\}\s*$',code)
    assert 'return' not in code and '#error PROBE_COUNT' in code
    for banned in ('CreateFile','GetProcAddress','Reg','socket','CreateProcess','fopen','malloc','system','WriteFile','GetProcAddress'):
        assert banned not in code


def test_pins_workflow_and_unchanged_inputs():
    pins=json.loads((D/'PROBE-SHA256.json').read_text())
    assert set(pins)=={'verification/windows_loader_probe/'+n for n in ('probe_template.c','probe_closure.py','decode_probe.py')}
    for path,pin in pins.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==pin,path
    for js in ('verification/windows_entry_marker/OBSERVATION-SHA256.json','verification/windows_substitute_exit77/SUBSTITUTE-SHA256.json'):
        for path,pin in json.loads((ROOT/js).read_text()).items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==pin,path
    w=(ROOT/'.github/workflows/windows-loader-probe-observation.yml').read_text()
    assert 'timeout-minutes: 15' in w and 'runs-on: windows-2022' in w and 'matrix:' not in w
    assert 'verification/windows_loader_probe/**' in w and 'windows_substitute_exit77/**' not in w and 'windows_entry_marker/**' not in w
    assert '$sub/stage_substitute.py' in w and 'stage_marker.py' not in w
    assert 'security_acceptance=NOT_ESTABLISHED' in w and 'negative_inconclusive=1' in w and 'diagnostic_only=1' in w
    assert 'verdict_rule=only_RAPS_or_profile_SID_counts' in w and 'model_only=1' in w
    assert w.index('probe_closure.py')<w.index('probe_template.c')<w.index('stage_substitute.py')<w.index('$safeCleanup=$false')<w.index('& pwsh -NoProfile -File')<w.index('decode_probe.py')<w.index('if(-not $safeCleanup)')<w.index('cleanup --root $owned')
    assert 'if($diagnosticExit -in @(0,122,123,124,126))' in w
    assert 'ALL_APPLICATION' not in w and 'icacls' not in w and 'logman' not in w and 'retry' not in w.lower()
    assert 'Probe closure refused before any launch' in w and 'Probe compile failed' in w
    assert 'probe_acl_model_error' in w and 'GetAccessRules($true,$true,[Security.Principal.SecurityIdentifier])' in w and 'Translate(' not in w
    dec=(D/'decode_probe.py').read_text()
    for limit in ('dont_resolve_maps_one_image_without_imports_or_DllMain=1','system32_dependencies_not_recursed=1','app_dir_shadowing_not_tested=1','known_dll_bits_say_nothing_about_acls=1'):assert limit in dec
