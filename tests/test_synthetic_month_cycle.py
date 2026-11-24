"""Standalone synthetic monthly cycle with actual encrypted archive prerequisite."""
import os
from datetime import date,timedelta
from decimal import Decimal
from io import BytesIO
import json

import pytest
from sqlalchemy import select,func,event
from openpyxl import load_workbook

from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_source_reconciliation import setup_source

pytestmark=pytest.mark.skipif(os.environ.get('MANUAL_SYNTHETIC_MONTH_CYCLE')!='1',reason='Dedicated synthetic_cycle DB, actual pg clients and committed fixture')


def test_source_month_real_backup_atomic_close_next_export_readonly_handoff(history_context,monkeypatch,tmp_path):
    from app.db import engine,SessionLocal
    from app.config import settings
    from app import backup_workflow as b
    from app.backup_crypto import set_passphrase
    from app.models import SystemSetting,EmployeeServicePeriod,Employee,LeaveMonthClose,LeaveRegisterEntry,AuditLog,BackupRun,PayrollItem,LeaveBalanceTransaction
    from app.leave_payroll_read import get_month_summary
    from app.leave_synthetic_close import close_synthetic_month
    from app.leave_register_review import RegisterEdit,prepare_register_edit
    from app.department_preparation_workflow import prepare_confirmed_source_department_export
    assert engine.url.database.startswith('synthetic_cycle_')
    monkeypatch.setattr(settings,'leave_synthetic_month_close_enabled',True)
    monkeypatch.setattr(settings,'app_env','test')
    backup_root=tmp_path/'backups';backup_root.mkdir()
    photos=tmp_path/'photos';photos.mkdir()
    monkeypatch.setattr(b,'PHOTO_DIRECTORY',photos)
    validate=b.validate_backup_root
    monkeypatch.setattr(b,'validate_backup_root',lambda value,require_external=True:validate(value,require_external=False))
    db,e,args=setup_source(history_context)
    auth={k:args[k] for k in ('actor_id','session_id','session_version','expected_epoch')}
    args['release_evaluated_on']=date(2026,2,28)
    db.add(SystemSetting(key='backup_root',value=str(backup_root),updated_by='synthetic'))
    set_passphrase(db,'synthetic-only-cycle-acceptance','synthetic')
    db.flush();db.commit()
    selected={k:v for k,v in args.items() if k not in auth}
    summary=get_month_summary(db,employee_id=e.id,year=2026,month=2,**args)
    close=dict(year=2026,month=2,employee_selections={e.id:selected},reviewed_summary_digests={e.id:summary.source_digest},
               reason='Synthetic monthly close only',human_confirmed=True,**auth)
    baseline=tuple(db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,PayrollItem))
    # Coverage can't silently close the global month for just one department.
    other=Employee(code='SYN-CYCLE-EXTRA',full_name='Synthetic extra',department_id=e.department_id,position='Synthetic',hire_date=date(2026,2,1),status='active',basic_salary=Decimal(1),daily_hours=Decimal(8))
    db.add(other);db.flush()
    service=EmployeeServicePeriod(employee_id=other.id,start_date=date(2026,2,1));db.add(service);db.flush()
    with pytest.raises(ValueError,match='Fresh read-only'):close_synthetic_month(db,**close)
    service_id=service.id
    db.commit()
    with pytest.raises(ValueError,match='every employee'):close_synthetic_month(db,**close)
    db.rollback()
    db.delete(db.get(EmployeeServicePeriod,service_id));db.commit()
    # Missing service for a hired employee is unresolved, never silently ignored.
    with pytest.raises(ValueError,match='Global source coverage incomplete'):close_synthetic_month(db,**close)
    db.rollback()
    # Remove only the synthetic extra fixture, not an actual employee workaround.
    db.delete(db.get(Employee,other.id));db.commit()
    # Fresh committed snapshot required.
    before=db.scalar(select(func.count()).select_from(LeaveMonthClose))
    def fail(session,ctx,objects):
        if any(isinstance(o,AuditLog) and o.action=='synthetic_leave_month_closed' for o in session.new):
            raise RuntimeError('Synthetic close audit failure')
    event.listen(db,'before_flush',fail)
    try:
        with pytest.raises(RuntimeError,match='audit failure'):close_synthetic_month(db,**close)
    finally:event.remove(db,'before_flush',fail)
    assert db.scalar(select(func.count()).select_from(LeaveMonthClose))==before
    db.rollback()  # prior failed savepoint assigned an xid, start fresh for archive snapshot
    # Tamper the actual persisted encrypted archive AFTER create_backup's own
    # verification. The close service must independently reject the exact bytes
    # and leave no close/audit record. Restore only this synthetic test file.
    original_create=b.create_backup
    damaged=[]
    def corrupt_after_creation(*a,**kw):
        run=original_create(*a,**kw)
        from pathlib import Path
        path=Path(run.archive_path);payload=path.read_bytes()
        damaged.append((path,payload))
        path.write_bytes(payload[:-1]+bytes((payload[-1]^1,)))
        return run
    monkeypatch.setattr(b,'create_backup',corrupt_after_creation)
    try:
        with pytest.raises(ValueError,match='hash'):
            close_synthetic_month(db,**close)
        assert db.scalar(select(func.count()).select_from(LeaveMonthClose))==before
    finally:
        for path,payload in damaged:path.write_bytes(payload)
        monkeypatch.setattr(b,'create_backup',original_create)
        db.rollback()
    row=close_synthetic_month(db,**close)
    snapshot=json.loads(row.policy_snapshot_json)
    assert snapshot['synthetic_only'] and not snapshot['availability_established'] and not snapshot['financial_effects']
    backup=db.get(BackupRun,row.backup_run_id)
    assert backup.status=='success' and backup.verified_at is not None and os.path.isfile(backup.archive_path)
    db.commit()
    edit=RegisterEdit(e.id,date(2026,2,28),'S',False,'Synthetic attempt','synthetic',date(2026,2,1),date(2026,2,28))
    assert 'source_period_contains_closed_month' in prepare_register_edit(db,edit).issues
    with pytest.raises(ValueError,match='already closed'):close_synthetic_month(db,**close)
    # The next-month artifact is still review-only planning, never available credit.
    export_args=dict(args)
    export_args.pop('release_evaluated_on')
    export_args['month_start']=date(2026,3,1)
    export_args['current_insurance_years']={(e.id,date(2026,m,1)):None for m in (1,2,3)}
    export_args['age_evaluation_dates']={date(2026,m,1):date(2026,m,1) for m in (1,2,3)}
    exported=prepare_confirmed_source_department_export(db,**export_args)
    assert exported.review_only and not exported.establishes_spendable_credit
    wb=load_workbook(BytesIO(exported.payload));assert wb.worksheets;wb.close()
    closed_summary=get_month_summary(db,employee_id=e.id,year=2026,month=2,**args)
    assert dict(closed_summary.codes)['A']==1 and not closed_summary.available_balance_established
    assert tuple(db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction,PayrollItem))==baseline
