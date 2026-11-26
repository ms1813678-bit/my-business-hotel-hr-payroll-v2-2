"""Portable ETW-trace packet controls with text fixtures. Never Windows execution or acceptance."""
from pathlib import Path
import hashlib
import importlib.util
import json
import re
import subprocess
import sys

import pytest

ROOT=Path(__file__).resolve().parents[1]
D=ROOT/'verification/windows_trace_diagnostic'
TOKEN='hony-trace-observe-123-1'


def load():
    spec=importlib.util.spec_from_file_location('analyze_trace',D/'analyze_trace.py')
    m=importlib.util.module_from_spec(spec);sys.modules['analyze_trace']=m;spec.loader.exec_module(m);return m


def xml_event(kind,pid,body='',extra=''):
    return '<Event xmlns="x"><System><Opcode>%s</Opcode></System><EventData><Data Name="ProcessId">0x%X</Data>%s</EventData>%s</Event>'%(kind,pid,body,extra)


def xml_dump(events):return '<Events>\n'+'\n'.join(events)+'\n</Events>\n'


def sample_xml(deny=True,irp=True):
    ev=[xml_event('Process Start',0x10,'<Data Name="ParentProcessId">0x4</Data><Data Name="ImageFileName">python.exe</Data><Data Name="CommandLine">C:\\\\x\\\\'+TOKEN+'\\\\staged\\\\runtime\\\\python.exe</Data>'),
        xml_event('Process Start',0x20,'<Data Name="ImageFileName">other.exe</Data><Data Name="CommandLine">'+TOKEN+' python.exe</Data>' .replace(' python.exe','')),
        xml_event('Process DCStart',0x30,'<Data Name="CommandLine">'+TOKEN+' python.exe</Data>')]
    for i in range(60):ev.append(xml_event('Image Load',0x10,'<Data Name="FileName">C:\\\\x\\\\dll%d.dll</Data>'%i))
    ev.append(xml_event('FileIO Create',0x10,'<Data Name="IrpPtr">0xABC</Data><Data Name="OpenPath">C:\\\\Windows\\\\System32\\\\secret.dll</Data>'))
    if deny:ev.append(xml_event('FileIO OpEnd',0x10,('<Data Name="IrpPtr">0xABC</Data>' if irp else '')+'<Data Name="NtStatus">0xC0000022</Data>'))
    ev.append(xml_event('Process End',0x10,'<Data Name="ExitStatus">0xC0000022</Data>'))
    ev.append(xml_event('FileIO Create',0x99,'<Data Name="NtStatus">0xC0000022</Data>'))   # other pid, never counted
    return xml_dump(ev)


def test_xml_layout_selects_only_command_line_token_python_start_not_dcstart():
    a=load();recs=a.records(sample_xml())
    assert a.pick_pids(recs,TOKEN)==[0x10]
    assert a.pid_of(recs[0])==0x10   # ProcessId, not ParentProcessId


def test_denied_open_with_matching_irp_and_caps_and_tail():
    a=load();out=a.analyze(sample_xml(),TOKEN)
    assert out[0].startswith('trace_counts total_events=') and 'child_pids=16' in out[0] and 'denied_events=1' in out[0] and 'trace_truncated=no' in out[0]
    assert sum(1 for l in out if l.startswith('trace_image_load'))==40 and 'dll59.dll' in [l for l in out if l.startswith('trace_image_load')][-1]
    assert any(l.startswith('trace_open') and 'secret.dll' in l for l in out)
    assert any(l.startswith('trace_denied') and '0xC0000022' in l for l in out)
    assert any(l.startswith('trace_process_stop') for l in out)
    assert any('trace_reading 1_denied_open_with_object' in l for l in out)
    assert 'security_acceptance=NOT_ESTABLISHED' in out[-1] and 'no_rerun=1' in out[-1]
    assert all(len(l)<=len('trace_denied ')+400+20 for l in out) and len(out)<=300


def test_denied_without_matching_irp_prints_denied_line_only():
    a=load();out=a.analyze(sample_xml(irp=False),TOKEN)
    assert not any(l.startswith('trace_open') for l in out) and any(l.startswith('trace_denied') for l in out)


def test_no_denied_event_reading_two_and_no_pid_reading_three_and_truncated_flag():
    a=load();out=a.analyze(sample_xml(deny=False),TOKEN,truncated=True)
    assert 'denied_events=0' in out[0] and 'trace_truncated=yes' in out[0] and any('trace_reading 2_no_denied' in l for l in out)
    out=a.analyze('<Events>\n'+xml_event('Process Start',7,'<Data Name="CommandLine">other.exe</Data>')+'\n</Events>',TOKEN)
    assert 'child_pids=none' in out[0] and 'pid_events=0' in out[0] and any('trace_reading 3_no_child_pid' in l for l in out)
    assert a.analyze('',TOKEN)[0].startswith('trace_counts total_events=0')


def test_text_key_value_alternate_layout():
    a=load()
    text='\n'.join(['Process, Start, ProcessId=0x2A, ImageFileName=python.exe, CommandLine=C:\\\\'+TOKEN+'\\\\runtime\\\\python.exe',
        'FileIO, Create, ProcessId=42, IrpPtr=0x77, OpenPath=C:\\\\Windows\\\\System32\\\\locked.dll',
        'FileIO, OpEnd, ProcessId: 42, IrpPtr: 0x77, NtStatus=0xC0000022',
        'Process, End, ProcessId=0x2A, ExitStatus=0xC0000022','junk line without pid'])
    out=a.analyze(text,TOKEN)
    assert 'child_pids=42' in out[0] and 'denied_events=1' in out[0] and any(l.startswith('trace_open') and 'locked.dll' in l for l in out)
    # a different key name does not select a PID, so the result is inconclusive, never guessed
    assert 'child_pids=none' in a.analyze(text.replace('ProcessId','Pxd'),TOKEN)[0]


def test_oversize_and_bad_token_refused_through_cli(tmp_path):
    a=load()
    dump=tmp_path/'d.xml';dump.write_text(sample_xml())
    ok=subprocess.run([sys.executable,str(D/'analyze_trace.py'),'--dump',str(dump),'--token',TOKEN],capture_output=True,text=True)
    assert ok.returncode==0 and 'trace_counts' in ok.stdout
    bad=subprocess.run([sys.executable,str(D/'analyze_trace.py'),'--dump',str(dump),'--token','a;b'],capture_output=True,text=True)
    assert bad.returncode==2 and 'bad_token' in bad.stdout
    miss=subprocess.run([sys.executable,str(D/'analyze_trace.py'),'--dump',str(tmp_path/'none'),'--token',TOKEN],capture_output=True,text=True)
    assert miss.returncode==2 and 'inconclusive=1' in miss.stdout
    a.MAX_BYTES=100
    big=tmp_path/'big.xml';big.write_text('x'*500)
    import types
    sys.argv=['x','--dump',str(big),'--token',TOKEN]
    assert a.main()==2
    with pytest.raises(a.Refused):a.analyze(sample_xml(),TOKEN,deadline=-1)


def test_output_line_cap_with_many_denied_events():
    a=load();ev=[xml_event('Process Start',0x10,'<Data Name="CommandLine">'+TOKEN+' python.exe</Data><Data Name="ImageFileName">python.exe</Data>')]
    for i in range(500):ev.append(xml_event('FileIO Create',0x10,'<Data Name="OpenPath">C:\\\\'+'p'*600+str(i)+'</Data><Data Name="NtStatus">0xC0000022</Data>'))
    out=a.analyze(xml_dump(ev),TOKEN)
    assert len(out)<=300 and 'denied_events=500' in out[0] and 'trace_denied_output_capped=1' in out and all(len(l)<=420 for l in out if l.startswith('trace_denied '))


def test_control_script_static_guards():
    t=(D/'trace_control.ps1').read_text()
    assert "logman start $script:TraceSession -p 'Windows Kernel Trace' $script:TraceKeywords -o $File -ets -max $script:TraceMaxMb" in t
    assert "'(process,thread,img,file,fileio,fileiocompletion,registry)'" in t and "'NT Kernel Logger'" in t and 'TraceMaxMb=64' in t
    assert t.count('logman stop')==1 and t.index('logman query -ets')<t.index('logman start')
    assert 'already exists' in t and 'IsInRole' in t and "GITHUB_ACTIONS -ne 'true'" in t
    assert 'session_gone' in t and 'still present after stop' in t
    for banned in ('sc.exe','New-Service','Set-Acl','auditpol','reg.exe','Set-ItemProperty','Invoke-WebRequest','curl','DownloadFile','bcdedit','gflags','Add-Type','icacls','takeown'):assert banned not in t


def test_pins_and_workflow_order_and_unchanged_flow():
    pins=json.loads((D/'TRACE-SHA256.json').read_text())
    assert set(pins)=={'verification/windows_trace_diagnostic/'+n for n in ('trace_control.ps1','analyze_trace.py')}
    for path,pin in pins.items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==pin,path
    for js in ('verification/windows_nodebug_diagnostic/OBSERVATION-SHA256.json','verification/windows_entry_marker/OBSERVATION-SHA256.json'):
        for path,pin in json.loads((ROOT/js).read_text()).items():assert hashlib.sha256((ROOT/path).read_bytes()).hexdigest()==pin,path
    w=(ROOT/'.github/workflows/windows-trace-observation.yml').read_text()
    base=(ROOT/'.github/workflows/windows-nodebug-observation.yml').read_text()
    assert 'timeout-minutes: 15' in w and 'runs-on: windows-2022' in w and 'matrix:' not in w and w.count('push:')==1
    assert 'verification/windows_trace_diagnostic/**' in w and 'windows_nodebug_diagnostic/**' not in w
    # real stage, real worker: the original stager call, no probe image, no substitute
    assert '$root/stage_runtime.py' in w and 'stage_substitute' not in w and 'stage_marker' not in w and 'probe.exe' not in w
    # every line of the original flow around the qualify call survives byte for byte except the owner_guard and trace insertions
    kept=[l for l in base.splitlines() if l.strip() and 'Remove-Item' not in l and 'New-Item -ItemType Directory $owned' not in l and not l.lstrip().startswith(('name:','- .github','- verification'))]
    missing=[l for l in kept if l not in w.splitlines()]
    assert missing==[ "      - name: Compile and qualify real parser candidate"] or all(l.strip().startswith(('$owned=Join-Path',)) or 'Compile and qualify' in l for l in missing),missing
    order=['Assert-TraceAllowed','Start-KernelTrace -File $etl','$safeCleanup=$false','& pwsh -NoProfile -File "$diag/qualify_diagnostic.ps1"','$diagnosticExit=$LASTEXITCODE','try{Stop-KernelTrace','tracerpt.exe','analyze_trace.py','if(-not $safeCleanup)','cleanup --root $owned']
    pos=[w.index(x) for x in order];assert pos==sorted(pos)
    assert w.index('diag_nodebug_child_exit')<w.index('try{Stop-KernelTrace;$traceStopped=$true}catch{Write-Output \'trace_stop_failed=1')
    fin=w[w.index('} finally {'):]
    assert fin.index('Stop-KernelTrace')<fin.index('if(-not $safeCleanup)')
    assert 'if($diagnosticExit -in @(0,122,123,124,126)){$safeCleanup=$true}' in w and 'trace_analysis_failed' in w
    assert "if($traceStarted -and -not $traceStopped){try{Stop-KernelTrace}catch" in w and 'WaitForExit(150000)' in w
    for banned in ('icacls','Set-Acl','gflags','ALL_APPLICATION','retry','Invoke-WebRequest','procmon','Procmon','wpr','xperf','cdb','Copy-Item','DownloadFile'):assert banned not in w if banned!='retry' else banned not in w.lower()


def test_numeric_start_opcode_selects_child_pid():
    import importlib.util
    spec=importlib.util.spec_from_file_location('analyze_trace',str(ROOT/'verification/windows_trace_diagnostic/analyze_trace.py'))
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    rec='<Event><Opcode>1</Opcode><Data Name="ProcessId">4242</Data><Data Name="ImageFileName">python.exe TOKENX</Data></Event>'
    assert m.pick_pids([rec],'tokenx')==[4242]
    dc=rec.replace('<Opcode>1</Opcode>','<Opcode>3</Opcode>')
    assert m.pick_pids([dc],'tokenx')==[]
