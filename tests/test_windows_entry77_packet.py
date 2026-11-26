"""Portable marker packet controls, never Windows execution or acceptance."""
from pathlib import Path
import importlib.util
import hashlib
import json
import shutil
import ast

import pytest

ROOT=Path(__file__).resolve().parents[1]
D=ROOT/'verification/windows_entry_marker'
S=ROOT/'verification/windows_integrated_candidate'


def load(name,file):
    spec=importlib.util.spec_from_file_location(name,D/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def setup(tmp_path):
    source=tmp_path/'original';source.mkdir();owned=tmp_path/'owned';owned.mkdir()
    (owned/'ENTRY77-OWNER.json').write_text(json.dumps({'owner':'synthetic-1','state':'preparing-entry77'}))
    for name in load('stager','stage_marker.py').SOURCE_PINS:shutil.copyfile(S/name,source/name)
    return source,owned


def test_source_copy_exact_only_worker_changed_original_untouched(tmp_path):
    module=load('stager','stage_marker.py');source,owned=setup(tmp_path)
    before={p.name:p.read_bytes() for p in source.iterdir()}
    copy=module.prepare(source,owned,'synthetic-1')
    assert set(p.name for p in copy.iterdir())==set(before)
    for name,data in before.items():
        assert (source/name).read_bytes()==data
        assert (copy/name).read_bytes()==(module.MARKER if name=='candidate_worker.py' else data)
    tree=ast.parse((copy/'candidate_worker.py').read_text())
    assert len(tree.body)==1 and isinstance(tree.body[0],ast.Raise)
    assert isinstance(tree.body[0].exc,ast.Call) and tree.body[0].exc.func.id=='SystemExit'
    assert ast.literal_eval(tree.body[0].exc.args[0])==77


@pytest.mark.parametrize('mode',['changed-source','missing-source','owner-mismatch','owner-state','occupied-copy','symlink-source','path-escape'])
def test_prepare_refuses_adversarial_inputs_without_original_mutation(tmp_path,mode):
    module=load('stager','stage_marker.py');source,owned=setup(tmp_path)
    if mode=='changed-source':(source/'stage_runtime.py').write_text('changed')
    if mode=='missing-source':(source/'candidate_worker.py').unlink()
    if mode=='owner-mismatch':(owned/'ENTRY77-OWNER.json').write_text('{"owner":"other","state":"preparing-entry77"}')
    if mode=='owner-state':(owned/'ENTRY77-OWNER.json').write_text('{"owner":"synthetic-1","state":"accepted"}')
    if mode=='occupied-copy':(owned/'entry77-source').mkdir()
    if mode=='symlink-source':
        original=source/'runtime_integrity.py';target=tmp_path/'target.py';original.rename(target)
        try:original.symlink_to(target)
        except OSError:pytest.skip('symlink creation unavailable')
    if mode=='path-escape':source=source/'..'/'original'
    before={p.name:p.read_bytes() for p in (tmp_path/'original').iterdir()}
    with pytest.raises((ValueError,OSError)):module.prepare(source,owned,'synthetic-1')
    assert {p.name:p.read_bytes() for p in (tmp_path/'original').iterdir()}==before


def test_partial_copy_failure_never_removes_owned_or_foreign_entries(monkeypatch,tmp_path):
    module=load('stager','stage_marker.py');source,owned=setup(tmp_path);foreign=owned/'keep';foreign.write_text('keep')
    original=Path.write_bytes
    def fail(path,data):
        if path.name=='stage_runtime.py' and path.parent.name=='entry77-source':raise OSError('synthetic')
        return original(path,data)
    monkeypatch.setattr(Path,'write_bytes',fail)
    with pytest.raises(OSError):module.prepare(source,owned,'synthetic-1')
    assert owned.is_dir() and foreign.read_text()=='keep' and (owned/'entry77-source').is_dir()


def test_actual_canonical_stager_from_copy_builds_marker_manifest(monkeypatch,tmp_path):
    module=load('stager','stage_marker.py');source,owned=setup(tmp_path);copy=module.prepare(source,owned,'synthetic-1')
    # Use actual byte-identical stager, no marker override/mode in its API.
    import sys
    monkeypatch.syspath_prepend(str(copy))
    spec=importlib.util.spec_from_file_location('entry_stage',copy/'stage_runtime.py');stager=importlib.util.module_from_spec(spec);spec.loader.exec_module(stager)
    runtime=tmp_path/'runtime';runtime.mkdir()
    for name in ('python.exe','python314.dll','python314.zip'):(runtime/name).write_bytes(b'synthetic')
    product=tmp_path/'product';(product/'app').mkdir(parents=True)
    for name in ('fingerprint_evidence.py','attendance_import.py'):(product/'app'/name).write_text('# synthetic')
    packages={}
    for name in ('openpyxl','xlrd','et_xmlfile'):
        q=tmp_path/'packages'/name;q.mkdir(parents=True);(q/'__init__.py').write_text('');packages[name]=q
    dest=owned/'staged';pin=stager.stage(runtime,product,packages,dest)
    integrity=sys.modules['runtime_integrity'];integrity.verify(dest,pin)
    assert (dest/'runtime/candidate_worker.py').read_bytes()==module.MARKER
    assert (copy/'stage_runtime.py').read_bytes()==(S/'stage_runtime.py').read_bytes()
    (dest/'runtime/candidate_worker.py').write_bytes(b'changed')
    with pytest.raises(ValueError):integrity.verify(dest,pin)


def test_additional_capture_strict_limits_and_no_echo():
    caller=load('entry_caller','diagnostic_caller.py')
    outer=b'diag_nodebug_outer status=refused child_exit=1 cleanup_confirmed=1 straggler_before_terminate=0 not_acceptance=1\n'
    timing=b'startup_process_exit api=1 error=0 code=77 still_active=0 times_api=1 times_error=0 kernel_100ns=123 user_100ns=456 before_cleanup_termination=1\n'
    post=b'diag_nodebug_post_resume_observation status=skipped_child_exited observation_only=1 fatal=0 held_event_verification=absent not_acceptance=1\n'
    facts=caller.diagnostic_fields(outer+timing+post)
    assert facts['capture']=='parsed' and facts['process_exit']=='77' and facts['kernel_100ns']=='123' and facts['post_resume']=='skipped_child_exited'
    for bad in (timing+timing,timing.replace(b'code=77',b'code=4294967296'),timing.replace(b'user_100ns=456',b'user_100ns=18446744073709551616'),post.replace(b'skipped_child_exited',b'SECRET/path')):
        assert caller.diagnostic_fields(outer+bad)['capture']=='rejected'
    assert 'SECRET' not in repr(caller.diagnostic_fields(outer+b'SECRET/path\n'))


def test_packet_workflow_and_pins_never_change_native_security_sources():
    pins=json.loads((D/'OBSERVATION-SHA256.json').read_text())
    for path,pin in pins.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==pin,path
    assert (D/'qualify_diagnostic.ps1').read_bytes()==(ROOT/'verification/windows_nodebug_diagnostic/qualify_diagnostic.ps1').read_bytes()
    text=(ROOT/'.github/workflows/windows-entry77-observation.yml').read_text()
    assert 'timeout-minutes: 15' in text and 'runs-on: windows-2022' in text
    assert 'verification/windows_entry_marker/**' in text and 'matrix:' not in text
    assert 'diag_entry77 changed_worker_only=1 marker=77 negative_inconclusive=1 security_acceptance=NOT_ESTABLISHED' in text
    assert '$nativeDiag/launcher_nodebug_diag.c' in text and '$nativeDiag/outer_watchdog.c' in text
    assert '$diag/stage_marker.py' in text and '--source $root --owned $owned' in text
    assert text.index('$safeCleanup=$false')<text.index('& pwsh -NoProfile -File')<text.index('if(-not $safeCleanup)')<text.index('cleanup --root $owned')
    assert 'if($diagnosticExit -in @(0,122,123,124,126))' in text
    assert 'loader_flag.ps1' not in text and 'logman' not in text


@pytest.mark.parametrize('mode',['normal','owner','identity','symlink','missing-marker'])
def test_owned_cleanup_only_exact_identity_and_owner(monkeypatch,tmp_path,mode):
    monkeypatch.syspath_prepend(str(D));guard=load('entry_guard','owner_guard.py')
    root=tmp_path/'owned';identity=guard.create(root,'test-1')
    (root/'regular').write_text('owned')
    outside=tmp_path/'outside';outside.mkdir();(outside/'keep').write_text('keep')
    if mode=='owner':(root/'ENTRY77-OWNER.json').write_text('{"owner":"foreign","state":"preparing-entry77"}')
    if mode=='identity':identity='0:0'
    if mode=='missing-marker':(root/'ENTRY77-OWNER.json').unlink()
    if mode=='symlink':
        try:(root/'escape').symlink_to(outside,target_is_directory=True)
        except OSError:pytest.skip('symlink unavailable')
    if mode=='normal':guard.cleanup(root,'test-1',identity);assert not root.exists()
    else:
        with pytest.raises((ValueError,OSError)):guard.cleanup(root,'test-1',identity)
        assert root.is_dir() and (root/'regular').read_text()=='owned'
    assert (outside/'keep').read_text()=='keep'
