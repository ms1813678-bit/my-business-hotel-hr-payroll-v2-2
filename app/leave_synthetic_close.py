"""Synthetic monthly-cycle close acceptance service, no HTTP or real closure.

A global host close must cover every employee in recorded service that month,
not merely one department. No closed-month rewrite/reopening/cash is provided.
"""
from dataclasses import asdict
from datetime import date,datetime,UTC
import json
import re
from sqlalchemy import select,text

from app.config import settings
from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.leave_payroll_read import get_month_summary
from app.models import EmployeeServicePeriod,LeaveMonthClose,PayrollRun,AuditLog
from app.leave_transition_draft import canonical


@bounded_workbook_operation
def close_synthetic_month(db,*,year,month,employee_selections,reviewed_summary_digests,
                          reason,human_confirmed,**identity):
    """Caller transaction, fresh real archive created/verified under source locks.

    Does NOT assert the independent old ledger has been reconciled or establish
    available balances. The immutable close snapshot records that limitation.
    """
    if (not settings.leave_synthetic_month_close_enabled
            or settings.app_env.strip().lower() not in {'test','synthetic'}
            or not (db.get_bind().url.database or '').startswith('synthetic_cycle_')):
        raise ValueError('Local synthetic month close is disabled.')
    if db.new or db.dirty or db.deleted:raise ValueError('Clean transaction required.')
    if (type(year) is not int or not 1<=year<=9999 or type(month) is not int or not 1<=month<=12
            or type(employee_selections) is not dict or not 1<=len(employee_selections)<=300
            or type(reviewed_summary_digests) is not dict or set(employee_selections)!=set(reviewed_summary_digests)
            or human_confirmed is not True or not isinstance(reason,str) or not reason.strip() or len(reason)>1000):
        raise ValueError('Explicit bounded complete month review required.')
    # pg_dump reads another connection: flushed uncommitted edits would be
    # absent from its archive even if this Session has no dirty ORM objects.
    if db.scalar(text('SELECT pg_current_xact_id_if_assigned()')) is not None:
        raise ValueError('Fresh read-only transaction required before archive-backed close.')
    actor=_guard(db,**identity)
    db.execute(text('LOCK TABLE employees,employee_service_periods,leave_register_entries,leave_month_notes,leave_records,leave_balance_transactions,leave_types,leave_month_close,payroll_runs IN SHARE ROW EXCLUSIVE MODE'))
    first=date(year,month,1)
    from calendar import monthrange
    last=date(year,month,monthrange(year,month)[1])
    if db.scalar(select(LeaveMonthClose.id).where(LeaveMonthClose.year==year,LeaveMonthClose.month==month)):
        raise ValueError('Month already closed; no silent replacement or reopen.')
    if db.scalar(select(PayrollRun.id).where(PayrollRun.period_start<=last,PayrollRun.period_end>=first,PayrollRun.status.in_(('approved','closed')))):
        raise ValueError('Approved/closed payroll blocks synthetic close.')
    employed=set(db.scalars(select(EmployeeServicePeriod.employee_id).where(EmployeeServicePeriod.start_date<=last,
        (EmployeeServicePeriod.end_date.is_(None)|(EmployeeServicePeriod.end_date>=first)))))
    if not employed or set(employee_selections)!=employed:
        raise ValueError('Global closure requires every employee in recorded service.')
    # Global preparation also catches hires with wholly missing service evidence
    # and target-month register rows outside the submitted service selection.
    # Being synthetic does not justify silently omitting an unresolved employee.
    from app.leave_month_close_readiness import prepare_month_close_readiness
    readiness=prepare_month_close_readiness(db,year=year,month=month,
        employee_selections=employee_selections,**identity)
    if not readiness['source_coverage_prepared']:
        raise ValueError('Global source coverage incomplete; review every unresolved employee.')
    summaries=[]
    for employee_id in sorted(employee_selections):
        selected=employee_selections[employee_id]
        if type(employee_id) is not int or employee_id<1 or type(selected) is not dict or set(selected)&set(identity):
            raise ValueError('Exact employee selections without authority overrides required.')
        result=get_month_summary(db,employee_id=employee_id,year=year,month=month,**selected,**identity)
        if reviewed_summary_digests[employee_id]!=result.source_digest:
            raise ValueError('Reviewed monthly source changed.')
        summaries.append(result)
    from app.backup_workflow import create_backup,verify_backup_archive
    from app.backup_crypto import load_data_key
    from app.leave_close_preview import BackupEvidence,validate_close_prerequisites
    with db.begin_nested():
        backup=create_backup(db,'manual',actor.username)
        if backup.status!='success' or not backup.archive_path:
            raise ValueError('Fresh verified archive required before close: '+str(backup.error_message))
        manifest=verify_backup_archive(backup.archive_path,backup.sha256,load_data_key(db))
        if manifest.get('database_name')!=db.get_bind().url.database:
            raise ValueError('Fresh backup database identity disagrees.')
        snapshot=dict(synthetic_only=True,source_summaries=[asdict(s) for s in summaries],
                      readiness_source_digest=readiness['source_digest'],
                      availability_established=False,financial_effects=False,
                      backup_sha256=backup.sha256,reason=reason.strip())
        # Canonical encoder handles exact Decimal/date evidence, then the host
        # validator validates the explicit nonempty snapshot and backup fields.
        serialized=validate_close_prerequisites(BackupEvidence(backup.id,backup.status,backup.verified_at,backup.archive_path,backup.sha256),json.loads(canonical(snapshot)))
        row=LeaveMonthClose(year=year,month=month,backup_run_id=backup.id,closed_by=actor.username,
                            closed_at=datetime.now(UTC),policy_snapshot_json=serialized)
        db.add(row);db.flush()
        db.add(AuditLog(actor=actor.username,action='synthetic_leave_month_closed',entity_type='leave_month_close',
            entity_id=str(row.id),new_value=serialized,reason=reason.strip()))
        db.flush()
    return row
