"""Integrated pending-manual-source to workbook/return review preparation.

Unexposed orchestration, no approval, accepted opening, registration, ledger or
payroll effect. Caller owns authenticated transaction, delivery and visual/UI
review. All policy/tier inputs remain explicit, never entitlement defaults.
"""
import json
from dataclasses import asdict, dataclass, replace
from datetime import date
from hashlib import sha256

from app.department_fingerprint_review import preview_department_fingerprint_review
from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_projection import projection_integrity
from app.department_workbook_source import read_workbook_source
from app.department_workbook_store import _guard, persist_export, prepare_return
from app.department_workbook_tiers import resolve_workbook_tiers
from app.leave_manual_opening import (
    build_manual_opening_preview_workbook,
    manual_opening_projection_preview,
)
from app.leave_manual_opening_groups import read_manual_opening_group


@dataclass(frozen=True)
class ManualDepartmentPreparationExport:
    export_id: str
    payload: bytes
    source_digest: str
    unresolved_target_days: tuple
    review_only: bool = True
    establishes_spendable_credit: bool = False


def _manual_department_projection(db, *, group_ids, department_id, month_start,
                                     policy, current_insurance_years, age_evaluation_dates,
                                     **identity):
    """Exact persisted pending groups + fresh locked source to persisted export.

    Not a normal accepted balance export. No missing opening category is zero.
    Group order is canonical; duplicate employees or mixed bases fail closed.
    Caller must label delivered artifact pending-source planning only.
    """
    if (type(group_ids) is not tuple or not 1<=len(group_ids)<=300
            or any(not isinstance(i,str) or len(i)!=64 for i in group_ids)
            or len(set(group_ids))!=len(group_ids)):
        raise ValueError('Explicit unique bounded persisted group selection required.')
    if type(month_start) is not date or month_start.day!=1:
        raise ValueError('Explicit first-of-month target required.')
    _guard(db, **identity)  # Exclusive gate before group/source reads or writes.
    reviews=[]
    for group_id in sorted(group_ids):
        reviews.extend(read_manual_opening_group(db, group_id=group_id, **identity))
    ids=tuple(sorted({r['employee_id'] for r in reviews}))
    if len(ids)!=len(group_ids):
        raise ValueError('Exactly one explicit selected group per employee required.')
    policy_digest=sha256(json.dumps(asdict(policy),sort_keys=True,separators=(',',':')).encode()).hexdigest()
    facts=read_workbook_source(db,department_id=department_id,employee_ids=ids,
        window_start=date(month_start.year,1,1),month_start=month_start,
        policy_digest=policy_digest,expected_epoch=identity['expected_epoch'])
    tiers=resolve_workbook_tiers(facts=facts,policy=policy,
        current_insurance_years=current_insurance_years,age_evaluation_dates=age_evaluation_dates)
    if tiers.unresolved:
        raise ValueError('Explicit tier evidence unresolved; no default or guessed entitlement.')
    projection=manual_opening_projection_preview(db=db,reviews=reviews,facts=facts,policy=policy,
        annual_tiers=tiers.tiers,planning_target_month=True)
    # Bind source identities, not only equivalent numeric opening values.
    source_digest=sha256(json.dumps({'projection':projection.source_digest,
        'pending_groups':sorted(group_ids),
        'proposals':sorted((r['proposal_id'],r['proposal_sha256']) for r in reviews)},
        sort_keys=True,separators=(',',':')).encode()).hexdigest()
    independent=json.loads(facts.canonical_json)
    independent.pop('policy_digest')
    independent['pending_groups']=sorted(group_ids)
    independent['proposals']=sorted((r['proposal_id'],r['proposal_sha256']) for r in reviews)
    independent_digest=sha256(json.dumps(independent,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    projection=replace(projection,source_digest=source_digest,policy_independent_source_sha256=independent_digest)
    projection=replace(projection,integrity_sha256=projection_integrity(projection))
    source=json.loads(facts.canonical_json)
    return projection, source['department'][0]['name']


@bounded_workbook_operation
def prepare_manual_department_export(db, **selection):
    projection, department = _manual_department_projection(db, **selection)
    payload,manifest=build_manual_opening_preview_workbook(projection=projection,
        department=department,month_start=selection['month_start'])
    identity={key:selection[key] for key in ('actor_id','session_id','session_version','expected_epoch')}
    row=persist_export(db,manifest=manifest,payload=payload,source_digest=projection.source_digest,**identity)
    return ManualDepartmentPreparationExport(row.id,payload,projection.source_digest,projection.unresolved_target_days)



@bounded_workbook_operation
def prepare_department_return_fingerprint_review(db, *, export_id, payload,
        fingerprint, group_ids, department_id, month_start, policy,
        current_insurance_years, age_evaluation_dates, **identity):
    """Persist clean workbook return evidence then join raw punches, no posting.

    Recomputes current source in this transaction from exact explicit prior
    group/policy/tier selection. No caller-provided digest freshness contract.
    Bad joins roll back receipt as well via savepoint.
    """
    from app.department_manifest import decode_manifest
    from app.department_workbook import inspect_department_return
    from app.models import DepartmentWorkbookExport
    with db.begin_nested():
        projection, _department = _manual_department_projection(db, group_ids=group_ids,
            department_id=department_id,month_start=month_start,policy=policy,
            current_insurance_years=current_insurance_years,age_evaluation_dates=age_evaluation_dates,**identity)
        row=prepare_return(db,export_id=export_id,payload=payload,
            current_source_digest=projection.source_digest,**identity)
        export=db.get(DepartmentWorkbookExport,export_id)
        returned=inspect_department_return(payload,decode_manifest(export.manifest_json))
        preview=preview_department_fingerprint_review(department=returned,fingerprint=fingerprint)
        return row.id,preview


@bounded_workbook_operation
def _confirmed_department_projection(db, *, selection_ids, department_id,
        month_start, current_insurance_years, age_evaluation_dates, **identity):
    """Consume exact current confirmed sources, not arbitrary pending selection.

    Still a planning workbook, not released/spendable balances or ledger rebasing.
    Existing conservative pending/not-payment warning is deliberately retained
    until full ledger/release reconciliation is implemented.
    """
    from app.leave_opening_source import read_confirmed_opening_source
    from app.models import LeaveOpeningSelection
    if (type(selection_ids) is not tuple or not 1 <= len(selection_ids) <= 300
            or len(set(selection_ids)) != len(selection_ids)):
        raise ValueError('Explicit unique confirmed selections required.')
    _guard(db, **identity)
    groups=[]; policy=None; policy_id=None; confirmed=[]
    for selection_id in selection_ids:
        _reviews, selected_policy, _digest=read_confirmed_opening_source(db,
            selection_id=selection_id,**identity)
        row=db.get(LeaveOpeningSelection,selection_id,populate_existing=True)
        if policy_id is not None and row.policy_id!=policy_id:
            raise ValueError('One explicit policy version per department preparation required.')
        groups.append(row.group_id);policy=selected_policy;policy_id=row.policy_id
        confirmed.append((row.id,row.source_sha256,row.revision))
    projection,department=_manual_department_projection(db,group_ids=tuple(groups),department_id=department_id,
        month_start=month_start,policy=policy,current_insurance_years=current_insurance_years,
        age_evaluation_dates=age_evaluation_dates,**identity)
    digest=sha256(json.dumps({'projection':projection.source_digest,'policy_id':policy_id,
        'confirmed_selections':sorted(confirmed)},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    projection=replace(projection,source_digest=digest)
    projection=replace(projection,integrity_sha256=projection_integrity(projection))
    return projection,department


@bounded_workbook_operation
def prepare_confirmed_source_department_export(db, **selection):
    projection,department=_confirmed_department_projection(db,**selection)
    payload,manifest=build_manual_opening_preview_workbook(projection=projection,
        department=department,month_start=selection['month_start'])
    identity={key:selection[key] for key in ('actor_id','session_id','session_version','expected_epoch')}
    export=persist_export(db,manifest=manifest,payload=payload,source_digest=projection.source_digest,**identity)
    return ManualDepartmentPreparationExport(export.id,payload,projection.source_digest,projection.unresolved_target_days)
