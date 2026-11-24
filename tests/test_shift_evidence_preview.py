from dataclasses import replace
from datetime import date,datetime
from hashlib import sha256
import pytest
from app.fingerprint_evidence import FingerprintEvidence
from app.shift_evidence_preview import ExplicitShiftWindow,preview_shift_occurrences


def window(key='night',day=1,start=23,endday=2,end=7):
    return ExplicitShiftWindow(key,1,date(2026,11,day),datetime(2026,11,day,start),datetime(2026,11,endday,end),0,0,'Synthetic explicit schedule')


def evidence(*times,issues=()):
    raw=b'synthetic-only';return FingerprintEvidence(sha256(raw).hexdigest(),raw,date(2026,11,1),date(2026,11,30),tuple(dict(employee_id=1,punch_at=t,sheet='Raw',row=i+2) for i,t in enumerate(times)),issues)


def preview(source,windows=None):
    return preview_shift_occurrences(employee_id=1,work_date=date(2026,11,1),windows=windows or (window(),),fingerprint=source)


def test_overnight_retains_duplicate_occurrences_no_direction_or_effect():
    result=preview(evidence(datetime(2026,11,1,23),datetime(2026,11,1,23),datetime(2026,11,2,7)))
    assert len(result.occurrences)==3 and [e['ordinal'] for e in result.occurrences]==[0,1,2]
    assert all(e['direction'] is None for e in result.occurrences)
    assert 'duplicate_occurrences_retained' in result.issues
    assert not result.establishes_direction and not result.eligible_for_registration and not result.establishes_overtime
    with pytest.raises(ValueError):replace(result,eligible_for_registration=True)


def test_shared_overnight_exit_and_next_shift_start_stays_unresolved():
    other=window('day',day=2,start=7,endday=2,end=15)
    result=preview(evidence(datetime(2026,11,2,7)),(window(),other))
    assert result.occurrences[0]['candidate_window_ids']==('night','day')
    assert 'occurrence_shared_between_windows' in result.issues
    assert result.occurrences[0]['direction'] is None


def test_missing_single_punch_never_fabricates_pair_or_absence():
    single=preview(evidence(datetime(2026,11,2,7)))
    assert len(single.occurrences)==1 and single.occurrences[0]['direction'] is None
    missing=preview(evidence())
    assert not missing.occurrences and 'no_occurrences_in_supplied_windows_not_absence' in missing.issues


def test_full_source_window_and_raw_provenance_change_digest():
    source=evidence(datetime(2026,11,1,23))
    first=preview(source)
    assert first.source_digest!=preview(source,(replace(window(),source_reference='Synthetic changed schedule'),)).source_digest
    changed=replace(source,occurrences=({**source.occurrences[0],'row':8},))
    assert first.source_digest!=preview(changed).source_digest
    with pytest.raises(ValueError):preview(replace(source,source_sha256='a'*64))


@pytest.mark.parametrize('change',[dict(employee_id=2),dict(before_minutes=True),dict(after_minutes=121),dict(scheduled_end=datetime(2026,11,1,22)),dict(source_reference='')])
def test_invalid_or_wrong_employee_windows_refused(change):
    with pytest.raises(ValueError):preview(evidence(),(replace(window(),**change),))


def test_unreadable_unknown_date_retained_not_clean_missing():
    result=preview(evidence(issues=({'employee_id':1,'reason':'invalid_or_missing_date','work_date':None,'sheet':'Raw','row':3},)))
    assert result.unreadable_evidence[0]['row']==3 and 'unreadable_source_evidence' in result.issues


def test_unknown_identity_within_window_retained_separate_not_assigned():
    source=evidence(datetime(2026,11,1,23))
    source=replace(source,occurrences=({**source.occurrences[0],'employee_id':None,'employee_code':'UNRESOLVED'},))
    result=preview(source)
    assert not result.occurrences and len(result.unresolved_identity_occurrences)==1
    assert result.unresolved_identity_occurrences[0]['raw_event']['employee_id'] is None
    assert 'unknown_identity_occurrences_not_assigned' in result.issues


@pytest.mark.parametrize('value',['2026-11-01',datetime(2026,11,1,8),'not-a-date'])
def test_untyped_issue_date_cannot_be_silently_dismissed(value):
    result=preview(evidence(issues=({'reason':'unresolved_employee_code','employee_id':None,'work_date':value},)))
    assert len(result.unreadable_evidence)==1 and 'unreadable_source_evidence' in result.issues


def test_other_employee_bad_timestamp_explicit_input_refusal_not_clean_result():
    source=evidence()
    source=replace(source,occurrences=({'employee_id':2,'punch_at':'malformed'},))
    with pytest.raises(ValueError,match='raw timestamp'):preview(source)


def test_overlap_detected_without_a_punch_and_nested_evidence_immutable():
    result=preview(evidence(),(window(),window('day',day=2,start=7,endday=2,end=15)))
    assert 'supplied_schedule_windows_overlap' in result.issues
    source=evidence(datetime(2026,11,1,23))
    result=preview(source)
    with pytest.raises(TypeError):result.occurrences[0]['raw_event']['row']=9
    source.occurrences[0]['row']=99
    assert result.occurrences[0]['raw_event']['row']==2


def test_unsupported_provenance_value_controlled_refusal():
    source=evidence(datetime(2026,11,1,23))
    source.occurrences[0]['bad']=object()
    with pytest.raises(ValueError,match='Unsupported'):preview(source)


def test_period_and_discard_diagnostics_digest_bound():
    source=evidence(datetime(2026,11,1,23),datetime(2026,11,5,8),issues=({'employee_id':1,'work_date':date(2026,11,9),'reason':'invalid_time_value'},))
    result=preview(source)
    assert result.diagnostic_counts['selected_outside_windows']==1
    assert result.diagnostic_counts['out_of_range_issues']==1
    assert result.source_digest!=preview(replace(source,period_end=date(2026,12,1))).source_digest


def test_original_global_ordinals_stable_across_aliases_employee_and_window_filters():
    from app.fingerprint_evidence import parse_fingerprint_evidence
    from app.shift_evidence_preview import preview_shift_occurrences,ExplicitShiftWindow
    from datetime import date,datetime
    raw=b'ID,Date,Time\nOTHER,2026-11-01,00:01\nPERSON,2026-11-01,08:00 08:00\nOTHER,2026-11-01,09:00\nPERSON,2026-11-01,16:00\n'
    def run(aliases,employee,start):
        evidence=parse_fingerprint_evidence(raw,filename='synthetic.csv',verified_aliases=aliases)
        window=ExplicitShiftWindow('explicit',employee,date(2026,11,1),datetime(2026,11,1,start),datetime(2026,11,1,18),0,0,'synthetic')
        return evidence,preview_shift_occurrences(employee_id=employee,work_date=date(2026,11,1),windows=(window,),fingerprint=evidence)
    evidence,both=run({'PERSON':1,'OTHER':2},1,8)
    _,late=run({'PERSON':1,'OTHER':2},1,12)
    _,other=run({'PERSON':1,'OTHER':2},2,8)
    changed,unknown=run({'PERSON':1},1,8)
    assert len(evidence.occurrences)==len(changed.occurrences)==5
    assert [v['ordinal'] for v in both.occurrences]==[1,2,4]
    assert [v['ordinal'] for v in late.occurrences]==[4]
    assert [v['ordinal'] for v in other.occurrences]==[3]
    assert [v['ordinal'] for v in unknown.occurrences]==[1,2,4]
    assert [v['ordinal'] for v in unknown.unresolved_identity_occurrences]==[3]
    assert [v['raw_event']['source_row'] for v in both.occurrences]==[3,3,5]
