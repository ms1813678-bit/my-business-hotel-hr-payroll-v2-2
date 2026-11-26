"""2خ: what the system does today when a POSTED leave month must change (tier/age
evidence, register edit, replay). Pins current behavior at 778196c: posted month is
immutable, every change path refuses with no write. There is NO correction/reopen
path for a posted leave month: that is a missing feature, not a tested one.
Synthetic employee, fixed dates, source amounts."""
from datetime import date
from decimal import Decimal
import pytest
from sqlalchemy import select, func
from app.leave_month_earning import review_month_earning, apply_month_earning
from app.leave_workflow import leave_balance
from app.models import LeaveBalanceTransaction, LeaveMonthEarning, LeaveRegisterEntry, AuditLog
from tests.test_leave_month_earning import monthly, post  # noqa: F401
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401


def snap(db, e, kinds):
    return (db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)),
            db.scalar(select(func.count()).select_from(LeaveMonthEarning)),
            {c: leave_balance(db, e.id, k) for c, k in kinds.items()})


def test_posted_month_cannot_be_reposted_with_changed_tier_evidence(monthly):
    db, e, kinds, args = monthly
    feb = args(date(2026, 2, 1), date(2026, 2, 28))
    post(db, feb)
    before = snap(db, e, kinds)
    changed = {**feb, 'current_insurance_years': {k: Decimal(10) for k in feb['current_insurance_years']}}
    rv = review_month_earning(db, **changed)   # review of a posted month still computes (observed)
    with pytest.raises(ValueError, match='already posted'):
        apply_month_earning(db, operation_id='d' * 64, reviewed_digest=rv['reviewed_digest'],
                            human_confirmed=True, reason='Synthetic tier change', **changed)
    assert snap(db, e, kinds) == before


def test_register_edit_after_posting_changes_nothing_and_cannot_repost(monthly):
    db, e, kinds, args = monthly
    feb = args(date(2026, 2, 1), date(2026, 2, 28))
    post(db, feb)
    before = snap(db, e, kinds)
    row = db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id == e.id,
                                                     LeaveRegisterEntry.work_date == date(2026, 2, 27)))
    row.code = 'A'; db.flush()
    assert snap(db, e, kinds) == before          # ledger does not follow the register
    with pytest.raises(ValueError, match='Prior earned source changed|already posted|correction required'):
        rv = review_month_earning(db, **feb)
        apply_month_earning(db, operation_id='e' * 64, reviewed_digest=rv['reviewed_digest'],
                            human_confirmed=True, reason='Synthetic edit', **feb)
    assert snap(db, e, kinds) == before


def test_no_audit_or_ledger_side_effect_from_refused_repost(monthly):
    db, e, kinds, args = monthly
    feb = args(date(2026, 2, 1), date(2026, 2, 28))
    rv, _ = post(db, feb)
    audits = db.scalar(select(func.count()).select_from(AuditLog))
    before = snap(db, e, kinds)
    with pytest.raises(ValueError, match='already posted'):
        apply_month_earning(db, operation_id='f' * 64, reviewed_digest=rv['reviewed_digest'],
                            human_confirmed=True, reason='Synthetic second try', **feb)
    assert snap(db, e, kinds) == before
    assert db.scalar(select(func.count()).select_from(AuditLog)) == audits
