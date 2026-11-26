"""Portable driver/control tests. No Windows process, ACL or MSVC execution proof."""
import importlib.util
from pathlib import Path
import sys
import types

import pytest

ROOT=Path(__file__).resolve().parents[1]
DIAG=ROOT/'verification/windows_nodebug_diagnostic'


def load(name, file):
    spec=importlib.util.spec_from_file_location(name,DIAG/file)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('status,code',[('returned',0),('refused',122),('output_refused',123),('decode_refused',123),('timeout',124),('unavailable',126),('unknown',125)])
def test_cli_exact_status_mapping(monkeypatch,status,code):
    class CleanupUnconfirmed(RuntimeError):pass
    caller=types.SimpleNamespace(CleanupUnconfirmed=CleanupUnconfirmed,
        observe_once=lambda **kw:dict(status=status,cleanup_confirmed=True,decoded=status=='returned'))
    monkeypatch.setitem(sys.modules,'diagnostic_caller',caller)
    monkeypatch.setitem(sys.modules,'app.fingerprint_bounded_parse',types.SimpleNamespace(_decode_worker=lambda *a:None))
    argv=['run_diagnostic.py']
    for name in ('watchdog','watchdog-pin','launcher','launcher-pin','profile','stage','manifest-pin','product-root'):
        argv.extend(['--'+name,str(ROOT)])
    monkeypatch.setattr(sys,'argv',argv)
    assert load('cli','run_diagnostic.py').main()==code


@pytest.mark.parametrize('outcome',[{'status':'returned','cleanup_confirmed':False,'decoded':True},None,RuntimeError('ambiguous')])
def test_cli_ambiguity_is_125(monkeypatch,outcome):
    class CleanupUnconfirmed(RuntimeError):pass
    def observe(**kw):
        if isinstance(outcome,Exception):raise outcome
        return outcome
    monkeypatch.setitem(sys.modules,'diagnostic_caller',types.SimpleNamespace(CleanupUnconfirmed=CleanupUnconfirmed,observe_once=observe))
    monkeypatch.setitem(sys.modules,'app.fingerprint_bounded_parse',types.SimpleNamespace(_decode_worker=lambda *a:None))
    argv=['run_diagnostic.py']
    for name in ('watchdog','watchdog-pin','launcher','launcher-pin','profile','stage','manifest-pin','product-root'):argv.extend(['--'+name,str(ROOT)])
    monkeypatch.setattr(sys,'argv',argv)
    assert load('cli','run_diagnostic.py').main()==125


def test_profile_and_workflow_both_default_retain_before_process():
    profile=(DIAG/'qualify_diagnostic.ps1').read_text()
    call=profile.index('& $TrustedPython -I -B')
    assert profile.index('$cleanupConfirmed=$false')<call
    assert profile.index('if($diagnosticExit -in @(0,122,123,124,126))',call)<profile.index('DeleteAppContainerProfile($profile)')
    assert 'if($created -and $cleanupConfirmed)' in profile
    assert "exit 125" in profile
    workflow=(ROOT/'.github/workflows/windows-nodebug-observation.yml').read_text()
    assert workflow.index('$safeCleanup=$false')<workflow.index('& pwsh -NoProfile -File')
    assert workflow.index('if(-not $safeCleanup)')<workflow.index('Remove-Item -LiteralPath $owned')
    assert 'if($diagnosticExit -in @(0,122,123,124,126))' in workflow
    assert 'loader_flag.ps1' not in workflow and 'Invoke-WithLoaderSnaps' not in workflow
    assert 'debugger_only_cause=NOT_ESTABLISHED' in workflow


def test_outer_atomic_job_deadline_and_accounting_source_contract():
    source=(DIAG/'outer_watchdog.c').read_text()
    assert source.index('PROC_THREAD_ATTRIBUTE_JOB_LIST,&job')<source.index('CreateProcessW(')
    assert source.index('IsProcessInJob(pi.hProcess,job')<source.index('ResumeThread(pi.hThread)')
    for token in ('CREATE_SUSPENDED','JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE','GetTickCount64()+15000','accounting.ActiveProcesses==0','result=125','GetTickCount64()+3000'):
        assert token in source
    assert 'AssignProcessToJobObject' not in source and 'BREAKAWAY_OK' not in source


def test_manifest_exact_sources():
    import json,hashlib
    pins=json.loads((DIAG/'OBSERVATION-SHA256.json').read_text())
    assert set(pins)=={'verification/windows_nodebug_diagnostic/'+f for f in ('launcher_nodebug_diag.c','outer_watchdog.c','diagnostic_caller.py','run_diagnostic.py','qualify_diagnostic.ps1')} | {'verification/windows_integrated_candidate/'+f for f in ('token_checks.h','startup_diagnostics.h','real_open_diagnostics.h')}
    assert all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in pins.items() if (ROOT/p).exists())


@pytest.mark.parametrize('exit_code,expected',[(0,'returned'),(122,'refused'),(123,'output_refused'),(124,'timeout'),(126,'unavailable'),(125,'unsafe'),(1,'unsafe'),(-1073741510,'unsafe')])
def test_actual_caller_native_exit_contract(monkeypatch,tmp_path,exit_code,expected):
    import contextlib,os
    caller=load('caller','diagnostic_caller.py')
    @contextlib.contextmanager
    def hold(path,*args):yield Path(path)
    monkeypatch.setitem(sys.modules,'runtime_integrity',types.SimpleNamespace(held_tree=hold))
    monkeypatch.setattr(caller,'held_binary',hold)
    # Replace module-local OS view only, never the interpreter's global os.name.
    monkeypatch.setattr(caller,'os',types.SimpleNamespace(name='nt',environ={'LOCALAPPDATA':'synthetic','SYSTEMROOT':'synthetic'},fstat=os.fstat))
    direct=types.SimpleNamespace(period_start='2026-11-01',period_end='2026-11-01',occurrences=[],issues=[])
    monkeypatch.setitem(sys.modules,'app.fingerprint_evidence',types.SimpleNamespace(parse_fingerprint_evidence=lambda *a,**kw:direct))
    class Child:
        def wait(self,timeout):return exit_code
        def __exit__(self,*args):return None
    calls=[]
    def popen(command,**kw):
        calls.append((command,kw));return Child()
    monkeypatch.setattr(caller.subprocess,'Popen',popen)
    invoke=lambda:caller.observe_once(watchdog=tmp_path/'outer.exe',watchdog_pin='a'*64,
        launcher=tmp_path/'inner.exe',launcher_pin='b'*64,profile='synthetic',
        staged_root=tmp_path,manifest_pin='c'*64,decoder=lambda *a:('2026-11-01','2026-11-01',[],[]))
    if expected=='unsafe':
        with pytest.raises(caller.CleanupUnconfirmed):invoke()
    else:
        result=invoke();assert result['status']==expected and result['cleanup_confirmed'] is True
    assert len(calls)==1
    command,kwargs=calls[0]
    assert kwargs['env']=={'LOCALAPPDATA':'synthetic','SYSTEMROOT':'synthetic'}
    assert kwargs['close_fds'] is True and kwargs['cwd']==str(tmp_path/'runtime')


@pytest.mark.parametrize('failure',['launch','wait'])
def test_actual_caller_launch_and_wait_ambiguity_retains(monkeypatch,tmp_path,failure):
    import contextlib,os,subprocess
    caller=load('caller','diagnostic_caller.py')
    @contextlib.contextmanager
    def hold(path,*args):yield Path(path)
    monkeypatch.setitem(sys.modules,'runtime_integrity',types.SimpleNamespace(held_tree=hold))
    monkeypatch.setattr(caller,'held_binary',hold)
    monkeypatch.setattr(caller,'os',types.SimpleNamespace(name='nt',environ={'LOCALAPPDATA':'synthetic','SYSTEMROOT':'synthetic'},fstat=os.fstat))
    direct=types.SimpleNamespace(period_start=None,period_end=None,occurrences=[],issues=[])
    monkeypatch.setitem(sys.modules,'app.fingerprint_evidence',types.SimpleNamespace(parse_fingerprint_evidence=lambda *a,**kw:direct))
    class Child:
        def wait(self,timeout):raise subprocess.TimeoutExpired('synthetic',timeout)
        def kill(self):pass
        def __exit__(self,*args):pass
    def popen(*a,**kw):
        if failure=='launch':raise OSError('synthetic refused')
        return Child()
    monkeypatch.setattr(caller.subprocess,'Popen',popen)
    with pytest.raises(caller.CleanupUnconfirmed):
        caller.observe_once(watchdog=tmp_path/'outer.exe',watchdog_pin='a'*64,
            launcher=tmp_path/'inner.exe',launcher_pin='b'*64,profile='synthetic',
            staged_root=tmp_path,manifest_pin='c'*64,decoder=lambda *a:None)


def test_workflow_has_one_top_level_on_mapping_and_no_request_parameter():
    text=(ROOT/'.github/workflows/windows-nodebug-observation.yml').read_text()
    assert sum(line.startswith('on:') for line in text.splitlines())==1
    assert '\non:\n  push:\n' in text
    assert '-Request ' not in text
    profile=(DIAG/'qualify_diagnostic.ps1').read_text()
    assert '[string]$Request' not in profile
    assert '& $TrustedPython -I -B' in profile


def test_isolated_cli_explicit_imports_without_ambient_pythonpath(tmp_path):
    import os,subprocess
    # Product source is supplied explicitly; -I must not depend on cwd/PYTHONPATH.
    product=ROOT
    command=[sys.executable,'-I','-B',str(DIAG/'run_diagnostic.py')]
    values={'watchdog':tmp_path/'outer.exe','watchdog-pin':'a'*64,'launcher':tmp_path/'inner.exe','launcher-pin':'b'*64,'profile':'synthetic','stage':tmp_path,'manifest-pin':'c'*64,'product-root':product}
    for key,value in values.items():command.extend(['--'+key,str(value)])
    # Linux only verifies import route and prelaunch unavailable mapping, no native execution.
    if os.name!='nt':
        run=subprocess.run(command,cwd=tmp_path,env={**os.environ,'PYTHONPATH':str(tmp_path/'ignored')},capture_output=True,text=True,timeout=10)
        assert run.returncode==126 and 'status=unavailable' in run.stdout
