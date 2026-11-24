"""Explicit schedule-window candidates for raw punches, never direction or attendance."""
from dataclasses import dataclass,field
from datetime import date,datetime,timedelta
from hashlib import sha256
import json
from types import MappingProxyType
from app.fingerprint_evidence import FingerprintEvidence
from app.time_semantics import local_elapsed_minutes_detail


@dataclass(frozen=True)
class ExplicitShiftWindow:
    selection_id:str
    employee_id:int
    work_date:date
    scheduled_start:datetime
    scheduled_end:datetime
    before_minutes:int
    after_minutes:int
    source_reference:str


@dataclass(frozen=True)
class ShiftOccurrencePreview:
    employee_id:int
    target_work_date:date
    windows:tuple
    occurrences:tuple
    unreadable_evidence:tuple
    identity_issues:tuple
    unresolved_identity_occurrences:tuple
    issues:tuple
    source_digest:str
    source_period_start:date|None
    source_period_end:date|None
    diagnostic_counts:object
    establishes_direction:bool=field(default=False,init=False)
    eligible_for_registration:bool=field(default=False,init=False)
    establishes_overtime:bool=field(default=False,init=False)


def preview_shift_occurrences(*,employee_id,work_date,windows,fingerprint):
    """All supplied windows include adjacent work dates to expose shared candidates.

    Caller-provided parsed objects are not verified against original bytes here.
    Only a source adapter that reparses bytes can establish parsed provenance.
    Windows are explicit inputs, not proof a roster is approved or complete.
    A punch in exactly one window still does not establish IN/OUT direction.
    Every occurrence has an ordinal, so identical timestamps remain distinct.
    """
    if type(employee_id) is not int or employee_id<1 or type(work_date) is not date:
        raise ValueError('Exact resolved employee and work date required.')
    if not isinstance(fingerprint,FingerprintEvidence):raise TypeError('Raw fingerprint evidence required.')
    if sha256(fingerprint.original_bytes).hexdigest()!=fingerprint.source_sha256:
        raise ValueError('Original fingerprint bytes and digest must agree.')
    windows=tuple(windows)
    if not 1<=len(windows)<=100 or len(fingerprint.occurrences)>200000 or len(fingerprint.issues)>20000:
        raise ValueError('Bounded explicit window evidence required.')
    ids=set();window_facts=[];issues=set()
    for window in windows:
        if not isinstance(window,ExplicitShiftWindow):raise TypeError('Explicit shift window required.')
        if (not isinstance(window.selection_id,str) or not 1<=len(window.selection_id)<=200 or window.selection_id in ids
            or window.employee_id!=employee_id or type(window.work_date) is not date
            or abs((window.work_date-work_date).days)>1
            or not isinstance(window.source_reference,str) or not 1<=len(window.source_reference.strip())<=500):
            raise ValueError('Unique employee-bound adjacent source windows required.')
        ids.add(window.selection_id)
        start,end=window.scheduled_start,window.scheduled_end
        if (type(start) is not datetime or type(end) is not datetime or start.tzinfo is not None or end.tzinfo is not None
            or start.date()!=window.work_date or end<=start or end-start>timedelta(days=1)):
            raise ValueError('Explicit ordered local schedule within one day required.')
        if any(type(m) is not int or not 0<=m<=120 for m in (window.before_minutes,window.after_minutes)):
            raise ValueError('Explicit bounded matching margins required.')
        minutes,basis=local_elapsed_minutes_detail(start,end)
        if basis!='exact':issues.add('schedule_duration_uncertain')
        window_facts.append(dict(selection_id=window.selection_id,employee_id=employee_id,work_date=window.work_date.isoformat(),
            scheduled_start=start.isoformat(),scheduled_end=end.isoformat(),before_minutes=window.before_minutes,
            after_minutes=window.after_minutes,source_reference=window.source_reference,elapsed_minutes=minutes,duration_basis=basis))
    if not any(w.work_date==work_date for w in windows):raise ValueError('Target workday window required.')
    for index,left in enumerate(windows):
        for right in windows[index+1:]:
            if max(left.scheduled_start-timedelta(minutes=left.before_minutes),right.scheduled_start-timedelta(minutes=right.before_minutes)) <= min(left.scheduled_end+timedelta(minutes=left.after_minutes),right.scheduled_end+timedelta(minutes=right.after_minutes)):
                issues.add('supplied_schedule_windows_overlap')
    occurrences=[];unresolved=[];seen=set()
    counts=dict(total_occurrences=len(fingerprint.occurrences),other_employee_occurrences=0,selected_outside_windows=0,unknown_outside_windows=0,duplicate_source_issues=0,other_employee_issues=0,out_of_range_issues=0)
    for ordinal,event in enumerate(fingerprint.occurrences):
        stamp=event.get('punch_at');identity=event.get('employee_id')
        if type(stamp) is not datetime or stamp.tzinfo is not None:raise ValueError('Explicit local raw timestamp required.')
        if identity is not None and (type(identity) is not int or identity<1):raise ValueError('Resolved identity or unknown required.')
        if identity not in (None,employee_id):
            counts["other_employee_occurrences"]+=1
            continue
        matches=tuple(w.selection_id for w in windows if w.scheduled_start-timedelta(minutes=w.before_minutes)<=stamp<=w.scheduled_end+timedelta(minutes=w.after_minutes))
        if not matches:
            counts["unknown_outside_windows" if identity is None else "selected_outside_windows"]+=1
            continue
        if identity is None:
            unresolved.append(MappingProxyType(dict(ordinal=ordinal,raw_event=dict(event),candidate_window_ids=matches,direction=None)))
            issues.add('unknown_identity_occurrences_not_assigned')
            continue
        if stamp in seen:issues.add('duplicate_occurrences_retained')
        seen.add(stamp)
        if len(matches)>1:issues.add('occurrence_shared_between_windows')
        occurrences.append(MappingProxyType(dict(ordinal=ordinal,raw_event=dict(event),candidate_window_ids=matches,direction=None)))
    unreadable=[];identity_issues=[]
    for issue in fingerprint.issues:
        if issue.get('reason')=='duplicate_punch':counts['duplicate_source_issues']+=1;continue
        if issue.get('employee_id') not in (None,employee_id):counts['other_employee_issues']+=1;continue
        day=issue.get('work_date')
        if type(day) is date and abs((day-work_date).days)>1:counts['out_of_range_issues']+=1;continue
        if issue.get('reason')=='unknown_employee':
            identity_issues.append(dict(issue));continue
        unreadable.append(dict(issue))
    if unreadable:issues.add('unreadable_source_evidence')
    if not occurrences:issues.add('no_occurrences_in_supplied_windows_not_absence')
    issues.update(('direction_and_completeness_unresolved','supplied_windows_not_roster_completeness_proof'))
    def serial(value):
        if isinstance(value,(date,datetime)):return value.isoformat()
        raise TypeError('Unsupported source value.')
    payload=dict(employee_id=employee_id,work_date=work_date.isoformat(),windows=window_facts,
        fingerprint_sha256=fingerprint.source_sha256,source_period_start=fingerprint.period_start,source_period_end=fingerprint.period_end,diagnostic_counts=counts,occurrences=[dict(v) for v in occurrences],unreadable=unreadable,identity_issues=identity_issues,
        unresolved_identity_occurrences=[dict(v) for v in unresolved],issues=sorted(issues))
    try:
        digest=sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),default=serial,allow_nan=False).encode()).hexdigest()
    except (TypeError,ValueError,OverflowError) as exc:raise ValueError('Unsupported evidence value.') from exc
    def freeze(value):
        if isinstance(value,dict):return MappingProxyType({k:freeze(v) for k,v in value.items()})
        if isinstance(value,(list,tuple)):return tuple(freeze(v) for v in value)
        return value
    occurrences=tuple(freeze(dict(v)) for v in occurrences)
    unresolved=tuple(freeze(dict(v)) for v in unresolved)
    unreadable=tuple(freeze(i) for i in unreadable)
    identity_issues=tuple(freeze(i) for i in identity_issues)
    return ShiftOccurrencePreview(employee_id,work_date,tuple(MappingProxyType(w) for w in window_facts),tuple(occurrences),
        tuple(unreadable),identity_issues,tuple(unresolved),tuple(sorted(issues)),digest,fingerprint.period_start,fingerprint.period_end,MappingProxyType(counts))
