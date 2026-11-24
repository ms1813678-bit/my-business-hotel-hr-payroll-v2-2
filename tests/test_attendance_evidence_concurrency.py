"""Dedicated disposable DB, two independent connections, actual commit/replay."""
import os
from datetime import date,datetime,UTC
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from sqlalchemy import select,func
from app.db import SessionLocal
from app.models import AdminAccount,AdminSession,AttendanceDailyRecord,AttendanceEvidenceReviewReceipt,AttendanceEvidenceConsumption,AuditLog,EmployeeServicePeriod,AttendanceMonth
from app.restore_lock import read_restore_epoch
from app.attendance_evidence_review import prepare_evidence_review,consume_evidence_review
from tests.test_shift_day_http import seed

pytestmark=pytest.mark.skipif(os.getenv('MANUAL_ATTENDANCE_CONCURRENCY')!='1',reason='Dedicated disposable database required')


def test_two_connection_same_key_changed_request_and_consume():
    employee,pattern=seed()
    with SessionLocal.begin() as db:
        actor=db.scalar(select(AdminAccount).where(AdminAccount.username=='synthetic-shift-system_admin'))
        db.add(EmployeeServicePeriod(employee_id=employee,start_date=date(2025,1,1)))
        db.add(AttendanceMonth(period_start=date(2026,11,1),period_end=date(2026,11,30),status='under_review',created_by=actor.username))
        sid='synthetic-concurrency-session';db.add(AdminSession(id=sid,admin_account_id=actor.id))
        auth=dict(actor_id=actor.id,session_id=sid,session_version=actor.session_version,expected_epoch=read_restore_epoch(db))
    args=dict(operation_id='f'*64,comparison_id='d'*64,employee_id=employee,work_date=date(2026,11,1),windows=((date(2026,11,1),pattern),),selected_window=f'2026-11-01:{pattern}',in_ordinal=0,out_ordinal=2,reason='Synthetic explicit concurrency direction review',**auth)
    def race(function):
        barrier=Barrier(2)
        def run(i):
            with SessionLocal.begin() as db:
                barrier.wait(timeout=5)
                return function(db,i)
        with ThreadPoolExecutor(max_workers=2) as pool:return list(pool.map(run,range(2)))
    result=race(lambda db,i:prepare_evidence_review(db,**args).request_digest)
    assert result[0]==result[1]
    def competing_new_key(db,i):
        try:
            row=prepare_evidence_review(db,**{**args,'operation_id':'3'*64,'reason':f'Synthetic competing explicit request {i}'})
            return ('saved',i,row.request_digest)
        except ValueError:return ('refused',i,None)
    competing=race(competing_new_key)
    assert sorted(v[0] for v in competing)==['refused','saved']
    winner=next(v for v in competing if v[0]=='saved')
    with SessionLocal() as check:
        import json
        durable=check.get(AttendanceEvidenceReviewReceipt,'3'*64)
        assert durable.request_digest==winner[2]
        assert json.loads(durable.proposal_json)['reason']==f'Synthetic competing explicit request {winner[1]}'
    result=race(lambda db,i:consume_evidence_review(db,receipt_id=args['operation_id'],confirmed_request_digest=result[0],**auth).id)
    assert result[0]==result[1]
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(AttendanceEvidenceReviewReceipt))==2
        assert db.scalar(select(func.count()).select_from(AttendanceEvidenceConsumption))==1
        assert db.scalar(select(func.count()).select_from(AttendanceDailyRecord))==1
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action.like('attendance_evidence_%')))==3
    def changed(db,i):
        try:return prepare_evidence_review(db,**{**args,'reason':'Different explicit review' if i else args['reason']}).id
        except ValueError:return 'changed-request-refused'
    changed_results=race(changed)
    assert changed_results==['f'*64,'changed-request-refused']


def test_restore_exclusive_gate_blocks_mutation_until_release():
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from app.restore_lock import RESTORE_LOCK_KEY
    with SessionLocal() as db:
        row=db.get(AttendanceEvidenceReviewReceipt,'f'*64)
        auth=dict(actor_id=row.actor_id,session_id=row.session_id,session_version=row.session_version,expected_epoch=row.restore_epoch)
        request=row.request_digest
    with SessionLocal() as blocker:
        blocker.execute(text('SELECT pg_advisory_lock(:key)'),{'key':RESTORE_LOCK_KEY})
        try:
            with SessionLocal() as worker:
                worker.execute(text("SET LOCAL lock_timeout='150ms'"))
                with pytest.raises(OperationalError):consume_evidence_review(worker,receipt_id='f'*64,confirmed_request_digest=request,**auth)
                worker.rollback()
        finally:blocker.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':RESTORE_LOCK_KEY})
    with SessionLocal.begin() as worker:
        record=consume_evidence_review(worker,receipt_id='f'*64,confirmed_request_digest=request,**auth)
        assert record.status=='needs_review'
