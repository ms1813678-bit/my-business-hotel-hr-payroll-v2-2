"""3ج replacement for the unconditional-skip placeholder in test_overtime_vs_attendance_extra.py.
Real migrated PostgreSQL. Pins CURRENT behavior at base 778196c, no core change:
 - a second attendance_daily_records row for the same employee/work_date is rejected by the DB;
 - the rejection leaves the first row and the day-award tables untouched;
 - a second day award for the same item/date through the service is refused (service-level).
Reuses the repo fixtures; skipped (UNRAN) unless the app DB is PostgreSQL.
"""
from datetime import date
from decimal import Decimal
import pytest
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from app.db import engine
from app.models import AttendanceDailyRecord, OvertimeDayAward
from app.overtime_day_award import prepare_day_award, confirm_day_award
from tests.test_overtime_day_source import attendance
from tests.test_pay_basis_source import setup
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_department_workbook_store import context  # noqa: F401

pytestmark = pytest.mark.skipif(engine.dialect.name != 'postgresql', reason='UNRAN: PostgreSQL required')
D = date(2026, 2, 2)


def _args(auth, item, **kw):
    return dict(operation_id='a' * 64, item_id=item.id, work_date=D, manual_hours=Decimal(3),
                rate_divisor=8, reason='Explicit excess grant reason', **auth) | kw


def test_duplicate_attendance_row_same_employee_day_rejected_by_db(history_context):
    db, auth, item, run = setup(history_context)
    first = attendance(db, item)
    n = db.scalar(select(func.count()).select_from(AttendanceDailyRecord).where(
        AttendanceDailyRecord.employee_id == first.employee_id, AttendanceDailyRecord.work_date == first.work_date))
    assert n == 1
    with pytest.raises(IntegrityError):
        with db.begin_nested():
            db.add(AttendanceDailyRecord(employee_id=first.employee_id, work_date=first.work_date, status='needs_review'))
            db.flush()
    assert db.scalar(select(func.count()).select_from(AttendanceDailyRecord).where(
        AttendanceDailyRecord.employee_id == first.employee_id, AttendanceDailyRecord.work_date == first.work_date)) == 1
    assert db.scalar(select(func.count()).select_from(OvertimeDayAward)) == 0


def test_second_day_award_same_item_day_refused_first_kept(history_context):
    db, auth, item, run = setup(history_context)
    attendance(db, item)
    row = prepare_day_award(db, **_args(auth, item))
    confirm_day_award(db, receipt_id=row.id, human_confirmed=True, **auth)
    with pytest.raises(ValueError, match='already awarded'):
        prepare_day_award(db, **_args(auth, item, operation_id='b' * 64, manual_hours=Decimal(5)))
    awards = db.scalars(select(OvertimeDayAward)).all()
    assert len(awards) == 1 and awards[0].manual_hours == 3
