"""Portable untrusted-diagnostic parser, never cleanup authority or OS proof."""
from pathlib import Path
import importlib.util
import re

import pytest

ROOT=Path(__file__).resolve().parents[1]
D=ROOT/'verification/windows_nodebug_diagnostic'
spec=importlib.util.spec_from_file_location('diagnostic_capture',D/'diagnostic_caller.py')
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
OUTER=b'diag_nodebug_outer status=refused child_exit=1 cleanup_confirmed=1 straggler_before_terminate=0 not_acceptance=1\n'


def test_all_current_refusal_stages_are_captured_exactly():
    source=(D/'launcher_nodebug_diag.c').read_text()
    actual=set(re.findall(r'stage="([a-z_]+)"',source))
    assert actual==c.REFUSAL_STAGES
    for stage in actual:
        raw=('CANDIDATE_REFUSAL stage='+stage+' error=5 child=3221225781\nCANDIDATE_CLEANUP complete=1 workload=refused security_acceptance=NOT_ESTABLISHED\n').encode()+OUTER
        fields=c.diagnostic_fields(raw)
        assert fields==dict(capture='parsed',outer_child='1',refusal_stage=stage,refusal_error='5',worker_child='3221225781',inner_cleanup='1',candidate_created='none',resume_prior='none',resume_failed='none',resume_error='none')


@pytest.mark.parametrize('raw',[
 OUTER+OUTER,OUTER+b'CANDIDATE_REFUSAL stage=SECRET/path error=1 child=0\n',
 OUTER+b'CANDIDATE_REFUSAL stage=job error=4294967296 child=0\n',
 OUTER+b'CANDIDATE_REFUSAL stage=job error=5 child=0 trailing=1\n',
 OUTER+b'CANDIDATE_CLEANUP complete=1 workload=returned security_acceptance=NOT_ESTABLISHED\n'*2,
 b'x'*66561,OUTER+b'\xff',b'no summary',OUTER.replace(b'child_exit=1',b'child_exit=4294967296'),
])
def test_malformed_duplicate_oversize_untrusted_fields_reject(raw):
    fields=c.diagnostic_fields(raw)
    assert fields['capture']=='rejected' and fields['outer_child']=='none'
    assert all('SECRET' not in value and '/' not in value for value in fields.values())


def test_unrelated_text_is_never_echoed_and_missing_refusal_is_explicit():
    result=c.diagnostic_fields(b'SECRET/path\n'+OUTER)
    assert result['capture']=='parsed' and result['refusal_stage']=='none'
    assert 'SECRET' not in repr(result)


def test_unsafe_result_still_refuses_after_capture():
    source=(D/'diagnostic_caller.py').read_text()
    capture=source.index("print('diag_nodebug_capture '")
    unsafe=source.index('if status is None or result == 125:')
    assert capture<unsafe<source.index("raise CleanupUnconfirmed('Watchdog tree cleanup unconfirmed')")
    assert 'if facts' not in source and "facts['cleanup" not in source


def test_created_and_resume_fields_drop_pid():
    raw=b'CANDIDATE_CREATED pid=123456 suspended=1\ndiag_nodebug_resume prior_suspend_count=1 api_failed=0 api_error=0 pre_resume_token_job_verified=1 debugger_attached=0 held_create_event_guarantee=absent\n'+OUTER
    facts=c.diagnostic_fields(raw)
    assert facts['candidate_created']=='1' and facts['resume_prior']=='1'
    assert facts['resume_failed']=='0' and facts['resume_error']=='0'
    assert '123456' not in repr(facts)
    assert c.diagnostic_fields(raw+raw)['capture']=='rejected'
