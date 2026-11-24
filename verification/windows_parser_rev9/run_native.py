"""Instinct: isolated native verification, no application imports or activation."""
import json,os,platform,subprocess,sys,xml.etree.ElementTree as ET
from pathlib import Path
ROOT=Path(__file__).resolve().parent
NATIVE=[
 'test_win_roundtrip_stdout','test_win_stdin_roundtrip_1mib_identical',
 'test_win_child_exits_without_reading_stdin_rejects_incomplete_input','test_win_wall_kill',
 'test_win_cpu_limit_kills_busy_loop_before_wall','test_win_memory_limit','test_win_output_cap',
 'test_win_no_orphan_processes_after_kill','test_win_active_process_limit_blocks_spawn',
 'test_win_pid_state_of_exited_process','test_win_child_sees_job_and_limits',
 'test_win_self_check_fails_outside_job','test_win_struct_size_accepted_and_reported_by_os']
def execute(args,label):
    # Each native case gets a fresh interpreter, so its autouse fixture cannot
    # erase retained handles from another case. Outer tree kill bounds a hang.
    p=subprocess.Popen([sys.executable,'-m','pytest','-q','-rs',*args],cwd=ROOT)
    try:code=p.wait(timeout=120)
    except subprocess.TimeoutExpired:
        subprocess.run(['taskkill','/PID',str(p.pid),'/T','/F'],check=False,timeout=20)
        p.wait(timeout=20)
        raise RuntimeError('Verification process tree timed out: '+label)
    return code

def main():
    assert sys.platform=='win32' and __import__('struct').calcsize('P')==8
    print(json.dumps({'platform':platform.platform(),'python':sys.version,'candidate_only':True}),flush=True)
    # Fake-API tests run separately and make no native-enforcement claim.
    failures=[]
    if execute(['test_candidate.py','-k','not test_win_','--junitxml=portable.xml'],'portable'):
        failures.append('portable')
    executed=0
    for name in NATIVE:
        xml=name+'.xml'
        if execute(['test_candidate.py::'+name,'--junitxml='+xml],name):failures.append(name)
        report=ROOT/xml
        if report.exists():
            cases=list(ET.parse(report).iter('testcase'))
            if len(cases)!=1 or any(c.find('skipped') is not None or c.find('failure') is not None or c.find('error') is not None for c in cases):
                if name not in failures:failures.append(name)
            else:executed+=1
        elif name not in failures:failures.append(name)
    print(json.dumps({'native_passed_without_skips':executed,'native_required':len(NATIVE),'failures':failures,'application_activated':False}),flush=True)
    return bool(failures)
if __name__=='__main__':raise SystemExit(main())
