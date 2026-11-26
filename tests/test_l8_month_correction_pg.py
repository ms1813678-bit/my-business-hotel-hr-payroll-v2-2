"""2خ (Mo answer 2, relayed by Main): correction of a POSTED leave month by reversal + replacement with a written
reason; the original receipt and its ledger rows are never edited or deleted. Real PG, synthetic, source amounts."""
from datetime import date
from fractions import Fraction
import pytest
from sqlalchemy import select, func, text
from sqlalchemy.exc import DBAPIError
from app.leave_cash_out import verified_released_balance
from app.leave_month_correction import (apply_month_correction, review_month_correction, verify_correction_receipt,
                                        verified_correction_ledger_ids)
from app.leave_month_earning import _verify_receipt
from app.leave_workflow import leave_balance
from app.models import (AuditLog, LeaveBalanceTransaction, LeaveMonthCorrection, LeaveMonthEarning)
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_month_earning import monthly, post  # noqa: F401
from tests.test_leave_cash_out import auth_of, payroll

REASON = 'Synthetic: wrong age tier, corrected with reversal'


def setup(monthly):
    db, e, kinds, args = monthly
    post(db, args(date(2026, 2, 1), date(2026, 2, 28)))      # W earned 4/7 posted, A held, A used 1
    auth = auth_of(args)
    return db, e, kinds, auth


def call(e, auth, corrected, **kw):
    return dict(employee_id=e.id, month_start=date(2026, 2, 1), corrected_earned=corrected,
                evaluated_on=kw.pop('evaluated_on', date(2026, 3, 1)), **auth)


def snap(db):
    return (db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)),
            db.scalar(select(func.count()).select_from(LeaveMonthCorrection)),
            db.scalar(select(func.count()).select_from(AuditLog)))


def test_reversal_plus_replacement_keeps_original_untouched_and_balance_exact(monthly):
    db, e, kinds, auth = setup(monthly)
    original_rows = [(r.id, r.source_operation_key, r.days_delta, r.exact_numerator, r.exact_denominator)
                     for r in db.scalars(select(LeaveBalanceTransaction).order_by(LeaveBalanceTransaction.id))]
    receipt = db.scalar(select(LeaveMonthEarning))
    assert leave_balance(db, e.id, kinds['W']) == Fraction(4, 7)
    rv = review_month_correction(db, reason=REASON, **call(e, auth, {'W': Fraction(3, 7)}))
    assert rv['posted'] is False and rv['posting_allowed'] is False
    assert rv['plan']['lines'] == [{'code': 'W', 'leave_type_id': kinds['W'], 'old': '4/7', 'new': '3/7'}]
    out = apply_month_correction(db, operation_id='a' * 64, reviewed_digest=rv['reviewed_digest'], human_confirmed=True,
                                 reason=REASON, **call(e, auth, {'W': Fraction(3, 7)}))
    assert [(i['role'], i['signed']) for i in out['ledger']] == [('reversal', '-4/7'), ('replacement', '3/7')]
    assert leave_balance(db, e.id, kinds['W']) == Fraction(3, 7)                     # 4/7 - 4/7 + 3/7
    after = [(r.id, r.source_operation_key, r.days_delta, r.exact_numerator, r.exact_denominator)
             for r in db.scalars(select(LeaveBalanceTransaction).order_by(LeaveBalanceTransaction.id))]
    assert after[:len(original_rows)] == original_rows and len(after) == len(original_rows) + 2   # nothing edited
    _verify_receipt(db, receipt)                                                         # original still verifies
    assert verify_correction_receipt(db, db.get(LeaveMonthCorrection, 'a' * 64)) == out
    assert verified_released_balance(db, employee_id=e.id, leave_type_id=kinds['W'], identity=auth)['balance'] == Fraction(3, 7)
    assert len(verified_correction_ledger_ids(db, e.id)) == 2


def test_exact_retry_is_identical_and_changed_request_is_refused(monthly):
    db, e, kinds, auth = setup(monthly)
    c = {'W': Fraction(3, 7)}
    rv = review_month_correction(db, reason=REASON, **call(e, auth, c))
    out = apply_month_correction(db, operation_id='b' * 64, reviewed_digest=rv['reviewed_digest'], human_confirmed=True, reason=REASON, **call(e, auth, c))
    before = snap(db)
    assert apply_month_correction(db, operation_id='b' * 64, reviewed_digest=rv['reviewed_digest'], human_confirmed=True, reason=REASON, **call(e, auth, c)) == out
    assert snap(db) == before
    with pytest.raises(ValueError, match='identity changed'):
        apply_month_correction(db, operation_id='b' * 64, reviewed_digest=rv['reviewed_digest'], human_confirmed=True, reason=REASON,
                               **call(e, auth, {'W': Fraction(1, 7)}))
    assert snap(db) == before


def test_second_correction_chains_from_effective_amount(monthly):
    db, e, kinds, auth = setup(monthly)
    for op, new in (('c' * 64, Fraction(3, 7)), ('d' * 64, Fraction(5, 7))):
        c = {'W': new}
        rv = review_month_correction(db, reason=REASON, **call(e, auth, c))
        apply_month_correction(db, operation_id=op, reviewed_digest=rv['reviewed_digest'], human_confirmed=True, reason=REASON, **call(e, auth, c))
    assert [r.sequence for r in db.scalars(select(LeaveMonthCorrection).order_by(LeaveMonthCorrection.sequence))] == [1, 2]
    assert leave_balance(db, e.id, kinds['W']) == Fraction(5, 7)


def test_refusals_write_nothing(monthly):
    db, e, kinds, auth = setup(monthly)
    before = snap(db)
    bad = [
        ({'W': Fraction(4, 7)}, 'nothing to correct'),                          # unchanged
        ({'A': Fraction(1, 2)}, 'Held annual'),                                 # held annual line
        ({'X': Fraction(1)}, 'Unknown leave category'),
        ({'W': Fraction(-1)}, 'nonnegative'),
        ({'W': 0.5}, 'exact nonnegative Fraction'),
    ]
    for corrected, msg in bad:
        with pytest.raises(ValueError, match=msg):
            review_month_correction(db, reason=REASON, **call(e, auth, corrected))
    with pytest.raises(ValueError, match='Written reason'):
        review_month_correction(db, reason='  ', **call(e, auth, {'W': Fraction(3, 7)}))
    with pytest.raises(ValueError, match='after the month'):
        review_month_correction(db, reason=REASON, **{**call(e, auth, {'W': Fraction(3, 7)}), 'evaluated_on': date(2026, 2, 10)})
    with pytest.raises(ValueError, match='Posted month receipt required'):
        review_month_correction(db, reason=REASON, **{**call(e, auth, {'W': Fraction(3, 7)}), 'month_start': date(2026, 1, 1)})
    rv = review_month_correction(db, reason=REASON, **call(e, auth, {'W': Fraction(3, 7)}))
    with pytest.raises(ValueError, match='confirmation'):
        apply_month_correction(db, operation_id='e' * 64, reviewed_digest=rv['reviewed_digest'], human_confirmed=False, reason=REASON, **call(e, auth, {'W': Fraction(3, 7)}))
    with pytest.raises(ValueError, match='review again'):
        apply_month_correction(db, operation_id='e' * 64, reviewed_digest='0' * 64, human_confirmed=True, reason=REASON, **call(e, auth, {'W': Fraction(3, 7)}))
    assert snap(db) == before


@pytest.mark.parametrize('status', ['approved', 'closed'])
def test_approved_or_closed_payroll_overlapping_the_month_refuses(monthly, status):
    db, e, kinds, auth = setup(monthly)
    payroll(db, e, status=status, start=date(2026, 2, 1), end=date(2026, 2, 28))
    db.flush()
    before = snap(db)
    with pytest.raises(ValueError, match='Approved/closed payroll'):
        review_month_correction(db, reason=REASON, **call(e, auth, {'W': Fraction(3, 7)}))
    assert snap(db) == before


def test_correction_receipt_is_immutable(monthly):
    db, e, kinds, auth = setup(monthly)
    c = {'W': Fraction(3, 7)}
    rv = review_month_correction(db, reason=REASON, **call(e, auth, c))
    apply_month_correction(db, operation_id='f' * 64, reviewed_digest=rv['reviewed_digest'], human_confirmed=True, reason=REASON, **call(e, auth, c))
    for sql in ("UPDATE leave_month_corrections SET sequence = 9", "DELETE FROM leave_month_corrections"):
        with pytest.raises(DBAPIError):
            with db.begin_nested():
                db.execute(text(sql))


def test_downward_correction_below_already_consumed_is_refused_and_writes_nothing(monthly):
    """W earned 4/7, 2/7 cashed out, balance 2/7: correcting W to 1/7 would need 3/7 back, so it refuses.
    Correcting to 2/7 (balance exactly 0) is allowed; upward is not blocked."""
    from tests.test_leave_cash_out import cash
    db, e, kinds, auth = setup(monthly)
    _run, item = payroll(db, e, status='draft')
    cash(db, auth, e, kinds['W'], item, '2/7', operation='9' * 64)
    assert leave_balance(db, e.id, kinds['W']) == Fraction(2, 7)
    before = snap(db)
    with pytest.raises(ValueError, match='already consumed'):
        review_month_correction(db, reason=REASON, **call(e, auth, {'W': Fraction(1, 7)}))
    with pytest.raises(ValueError, match='already consumed'):
        apply_month_correction(db, operation_id='b' * 64, reviewed_digest='1' * 64, human_confirmed=True,
                               reason=REASON, **call(e, auth, {'W': Fraction(1, 7)}))
    assert snap(db) == before
    ok = review_month_correction(db, reason=REASON, **call(e, auth, {'W': Fraction(2, 7)}))      # balance lands on 0
    assert ok['plan']['lines'][0]['new'] == '2/7'
    up = review_month_correction(db, reason=REASON, **call(e, auth, {'W': Fraction(5, 7)}))
    assert up['plan']['lines'][0]['new'] == '5/7'


def test_closed_leave_month_refuses_review_and_apply_without_writes(monthly):
    from app.models import BackupRun, LeaveMonthClose, utcnow
    db, e, kinds, auth = setup(monthly)
    rv = review_month_correction(db, reason=REASON, **call(e, auth, {'W': Fraction(3, 7)}))
    backup = BackupRun(backup_type='manual', status='success', archive_path='synthetic-only', size_bytes=1, initiated_by='synthetic')
    db.add(backup); db.flush()
    db.add(LeaveMonthClose(year=2026, month=2, backup_run_id=backup.id, closed_by='synthetic', closed_at=utcnow(), policy_snapshot_json='{}'))
    db.flush()
    before = snap(db)
    with pytest.raises(ValueError, match='Closed leave month'):
        review_month_correction(db, reason=REASON, **call(e, auth, {'W': Fraction(3, 7)}))
    with pytest.raises(ValueError, match='Closed leave month'):
        apply_month_correction(db, operation_id='c' * 64, reviewed_digest=rv['reviewed_digest'], human_confirmed=True,
                               reason=REASON, **call(e, auth, {'W': Fraction(3, 7)}))
    assert snap(db) == before
