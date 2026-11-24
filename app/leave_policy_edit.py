"""Durable HR policy edit preview and explicit version save, no rebinding."""
from datetime import UTC,datetime
from hashlib import sha256
import json,re,secrets
from sqlalchemy import select
from app.department_workbook_store import _guard
from app.leave_policy_store import canonical_policy,read_policy_version,persist_policy_version,policy_read_guard
from app.leave_policy_preview import LeavePolicy
from app.models import LeavePolicyEditDraft
from app.department_workbook_guard import bounded_workbook_operation


def parse_policy_fields(fields):
    expected={'tier30_by_insurance_years','tier30_by_age_50','annual_deduct_unpaid_absence','annual_deduct_sick',
        'annual_year_basis','cash_basis','holiday_credit_during_sick','sick_alert_thresholds'}
    if type(fields) is not dict or set(fields)!=expected:raise ValueError('Complete exact policy fields required.')
    values={}
    for key in expected-{'annual_year_basis','cash_basis','sick_alert_thresholds'}:
        if type(fields[key]) is not str or fields[key] not in ('true','false'):raise ValueError('Explicit true/false policy field required.')
        values[key]=fields[key]=='true'
    if fields['annual_year_basis']!='calendar':raise ValueError('Current confirmed annual basis is calendar.')
    if fields['cash_basis'] not in ('prorated','full_tier'):raise ValueError('Explicit cash basis required.')
    raw=fields['sick_alert_thresholds']
    if not isinstance(raw,str) or re.fullmatch(r'[1-9][0-9]{0,5}(?:,[1-9][0-9]{0,5}){0,19}',raw) is None:
        raise ValueError('Bounded distinct positive integer alert thresholds required.')
    thresholds=tuple(int(v) for v in raw.split(','))
    if len(set(thresholds))!=len(thresholds):raise ValueError('Duplicate alert threshold.')
    values.update(annual_year_basis='calendar',cash_basis=fields['cash_basis'],sick_alert_thresholds=tuple(sorted(thresholds)))
    return LeavePolicy(**values)


def _policy(text):
    raw=json.loads(text);raw['sick_alert_thresholds']=tuple(raw['sick_alert_thresholds'])
    value=LeavePolicy(**raw)
    if canonical_policy(value)!=text:raise ValueError('Canonical draft policy required.')
    return value


def _digest(row):
    text=json.dumps(dict(actor_id=row.actor_id,base_policy_id=row.base_policy_id,base_sha256=row.base_sha256,
        policy_json=row.policy_json,reason=row.reason,restore_epoch=row.restore_epoch),sort_keys=True,separators=(',',':'))
    return sha256(text.encode()).hexdigest()


@bounded_workbook_operation
def prepare_policy_edit(db,*,base_policy_id,fields,reason,**identity):
    account=_guard(db,**identity)
    if not isinstance(reason,str) or not reason.strip() or len(reason)>1000:raise ValueError('Bounded review reason required.')
    before=read_policy_version(db,policy_id=base_policy_id,**identity)
    after=parse_policy_fields(fields)
    row=LeavePolicyEditDraft(id=secrets.token_hex(32),actor_id=account.id,base_policy_id=base_policy_id,
        base_sha256=sha256(canonical_policy(before).encode()).hexdigest(),policy_json=canonical_policy(after),reason=reason.strip(),
        restore_epoch=identity['expected_epoch'],created_at=datetime.now(UTC),review_sha256='')
    row.review_sha256=_digest(row);db.add(row);db.flush()
    return row


@bounded_workbook_operation
def read_policy_edit(db,*,draft_id,**identity):
    policy_read_guard(db,**identity)
    row=db.get(LeavePolicyEditDraft,draft_id,populate_existing=True)
    if row is None or row.actor_id!=identity['actor_id'] or row.restore_epoch!=identity['expected_epoch'] or _digest(row)!=row.review_sha256:
        raise ValueError('Current owner-bound policy preview required.')
    before=read_policy_version(db,policy_id=row.base_policy_id,**identity)
    if sha256(canonical_policy(before).encode()).hexdigest()!=row.base_sha256:raise ValueError('Base policy changed.')
    try:after=_policy(row.policy_json)
    except (TypeError,KeyError,AttributeError) as exc:raise ValueError('Malformed policy draft.') from exc
    return row,before,after


@bounded_workbook_operation
def confirm_policy_edit(db,*,draft_id,reviewed_sha256,human_confirmed,**identity):
    _guard(db,**identity)
    db.execute(select(LeavePolicyEditDraft.id).where(LeavePolicyEditDraft.id==draft_id).with_for_update())
    row,before,after=read_policy_edit(db,draft_id=draft_id,**identity)
    if human_confirmed is not True or reviewed_sha256!=row.review_sha256:raise ValueError('Explicit reviewed policy confirmation required.')
    if row.saved_policy_id:
        saved=read_policy_version(db,policy_id=row.saved_policy_id,**identity)
        if saved!=after:raise ValueError('Saved policy mismatch.')
        return row.saved_policy_id
    saved=persist_policy_version(db,policy=after,reason=row.reason,**identity)
    row.saved_policy_id=saved.id;db.flush()
    return saved.id
