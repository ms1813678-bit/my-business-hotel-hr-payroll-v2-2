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
    for package in ('et_xmlfile','openpyxl','xlrd'):
        origin=tmp_path/'runtime/vendor'/package/'__init__.py'
        origin.parent.mkdir(parents=True,exist_ok=True);origin.write_text('')
        monkeypatch.setitem(sys.modules,package,types.SimpleNamespace(__file__=str(origin)))
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
    for package in ('et_xmlfile','openpyxl','xlrd'):
        origin=tmp_path/'runtime/vendor'/package/'__init__.py'
        origin.parent.mkdir(parents=True,exist_ok=True);origin.write_text('')
        monkeypatch.setitem(sys.modules,package,types.SimpleNamespace(__file__=str(origin)))
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


def test_unsafe_native_code_is_logged_before_both_retention_decisions():
    profile=(DIAG/'qualify_diagnostic.ps1').read_text()
    assert profile.index('diag_nodebug_host_exit code=')<profile.index('if($diagnosticExit -in')
    workflow=(ROOT/'.github/workflows/windows-nodebug-observation.yml').read_text()
    assert workflow.index('diag_nodebug_child_exit code=')<workflow.index('if($diagnosticExit -in')
    assert 'if($created -and $cleanupConfirmed)' in profile
    assert workflow.index('if(-not $safeCleanup)')<workflow.index('Remove-Item -LiteralPath $owned')


@pytest.mark.parametrize('failure,launch', [('preflight','0'),('popen','unknown'),('wait','1')])
def test_cli_stage_launch_state_and_no_exception_text(monkeypatch,capsys,failure,launch):
    class CleanupUnconfirmed(RuntimeError):pass
    def observe(**kw):
        kw['progress']('integrity_holds')
        if failure=='preflight':raise ModuleNotFoundError('SECRET/path')
        kw['progress']('watchdog_launch')
        if failure=='popen':raise CleanupUnconfirmed('SECRET/path')
        kw['progress']('watchdog_started')
        kw['progress']('watchdog_wait')
        raise CleanupUnconfirmed('SECRET/path')
    monkeypatch.setitem(sys.modules,'diagnostic_caller',types.SimpleNamespace(CleanupUnconfirmed=CleanupUnconfirmed,observe_once=observe))
    monkeypatch.setitem(sys.modules,'app.fingerprint_bounded_parse',types.SimpleNamespace(_decode_worker=lambda *a:None))
    argv=['run_diagnostic.py']
    for name in ('watchdog','watchdog-pin','launcher','launcher-pin','profile','stage','manifest-pin','product-root'):argv.extend(['--'+name,str(ROOT)])
    monkeypatch.setattr(sys,'argv',argv)
    assert load('cli','run_diagnostic.py').main()==125
    out=capsys.readouterr().out
    assert 'launch_began='+launch in out and 'native_exit=none' in out
    assert 'SECRET' not in out and '/path' not in out


def test_vendor_preflight_rejects_ambient_module_before_popen(monkeypatch,tmp_path):
    import contextlib,os
    caller=load('caller','diagnostic_caller.py')
    @contextlib.contextmanager
    def hold(path,*args):yield Path(path)
    monkeypatch.setitem(sys.modules,'runtime_integrity',types.SimpleNamespace(held_tree=hold))
    monkeypatch.setattr(caller,'held_binary',hold)
    monkeypatch.setattr(caller,'os',types.SimpleNamespace(name='nt',environ={'LOCALAPPDATA':'synthetic','SYSTEMROOT':'synthetic'},fstat=os.fstat))
    wrong=tmp_path/'ambient/__init__.py';wrong.parent.mkdir();wrong.write_text('')
    vendor=tmp_path/'runtime/vendor/et_xmlfile';vendor.mkdir(parents=True)
    monkeypatch.setitem(sys.modules,'et_xmlfile',types.SimpleNamespace(__file__=str(wrong)))
    def forbidden(*a,**kw):raise AssertionError('Popen forbidden before verified vendor preflight')
    monkeypatch.setattr(caller.subprocess,'Popen',forbidden)
    stages=[]
    with pytest.raises(ValueError,match='vendor origin refused'):
        caller.observe_once(watchdog=tmp_path/'outer.exe',watchdog_pin='a'*64,
            launcher=tmp_path/'inner.exe',launcher_pin='b'*64,profile='synthetic',
            staged_root=tmp_path,manifest_pin='c'*64,decoder=lambda *a:None,progress=lambda stage,code:stages.append(stage))
    assert stages==['request','integrity_holds','package_preflight']


def test_cli_import_failure_is_reported_with_no_raw_details(tmp_path):
    import subprocess
    command=[sys.executable,'-I','-S','-B',str(DIAG/'run_diagnostic.py')]
    for key,value in {'watchdog':'x','watchdog-pin':'a'*64,'launcher':'x','launcher-pin':'b'*64,'profile':'synthetic','stage':tmp_path,'manifest-pin':'c'*64,'product-root':tmp_path/'SECRET_MISSING_ROOT'}.items():command.extend(['--'+key,str(value)])
    run=subprocess.run(command,cwd=tmp_path,capture_output=True,text=True,timeout=10)
    assert run.returncode==125
    assert 'diag_nodebug_failure stage=cli_import kind=module_missing launch_began=0 native_exit=none' in run.stdout
    assert 'SECRET' not in run.stdout and str(tmp_path) not in run.stdout
