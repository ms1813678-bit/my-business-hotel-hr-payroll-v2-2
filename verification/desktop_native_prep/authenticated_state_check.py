"""Read-only post-window assertions for owned synthetic native authenticated fixture."""
import json,os,pathlib,sys
from decimal import Decimal
if sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('disposable native CI only')
root=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(root/'source'))
seed=json.loads((root/'auth-seed.json').read_text())
from sqlalchemy import select,func
from app.db import SessionLocal
from app.models import PayrollItem,PayrollRun,PayrollPayment,AuditLog,AdminSession
with SessionLocal() as db:
    run=db.get(PayrollRun,seed['payroll_run_id']);item=db.get(PayrollItem,seed['payroll_item_id'])
    assert run.status=='draft' and run.approved_by is None
    assert item.other_bonus==Decimal('0') and item.absence_days==Decimal('1') and item.net_pay==Decimal('5806.45')
    assert not item.absence_leave_reviewed and item.absence_leave_review_fingerprint is None
    assert db.scalar(select(func.count()).select_from(PayrollPayment))==0
    assert db.scalar(select(func.count()).select_from(AdminSession))==1
    assert sorted(db.scalars(select(AuditLog.action)).all())==['payroll_item_updated','payroll_run_created']
    assert not list((root/'source/var/reports').glob('*'))
print('NATIVE_AUTH_PAGES_READONLY_FINANCIAL_STATE_SESSION_PASS',flush=True)
