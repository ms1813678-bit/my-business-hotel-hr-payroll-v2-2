"""Concrete consumers over real migrated PostgreSQL rows holding 1/7 and high-scale finite amounts.

Real identity/epoch fixtures (history_context), no mocks for rows, no gate bypass,
no skip/xfail. Tests assert the INTENDED behaviour of each consumer.
The overlap tests once failed because app/leave_overlap_evidence.py encode() could not
serialize Fraction; they pass now (all tests in this file pass on PostgreSQL 16, 27 Nov 2026).
"""
import json
from datetime import date
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
from io import BytesIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import func, select

from app.ai_agent import execute_plan
from app.ai_gateway import build_confirmation_preview
from app.department_workbook_source import read_workbook_source
from app.leave_exact_amount import canonical_amount, exact_sum
from app.leave_exact_read import exact_ledger_balance
from app.leave_ledger_read import grouped_ledger_balances
from app.leave_overlap_evidence import classify_record_overlap, read_overlap_candidates
from app.leave_source_reconciliation import reconcile_selected_opening
from app.leave_workflow import create_leave_draft, leave_balance
from app.models import AuditLog, LeaveBalanceTransaction, LeaveRegisterEntry, utcnow
from app.reports import build_hr_workbook
from tests.leave_rational_consumer_support import (  # noqa: F401  (fixtures + helpers)
    HIGH_SCALE_MANUAL, MIXED_FINITE, ONE_SEVENTH, PLAIN_FINITE,
    consumer_store, finite_row, history_context, rational_row,
)
from tests.test_leave_source_reconciliation import setup_source
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401

STALE = 'تغير رصيد الإجازة'  # prefix of the stale-confirmation error in execute_plan


def adjust_plan(employee, kind, days):
    return {'action': 'adjust_leave_balance', 'employee_id': employee.id,
            'leave_type_id': kind.id, 'balance_days': days, 'transaction_type': 'adjustment',
            'reason': 'Synthetic consumer proof'}


def confirmed(db, plan):
    return {**plan, '_confirmation_snapshot': build_confirmation_preview(plan, session=db)['snapshot']}


def canon(fraction):
    return f'{fraction.numerator}/{fraction.denominator}'


def ledger_count(db, employee, kind):
    return db.scalar(select(func.count()).select_from(LeaveBalanceTransaction).where(
        LeaveBalanceTransaction.employee_id == employee.id,
        LeaveBalanceTransaction.leave_type_id == kind.id))


# 1 -------------------------------------------------------------- AI preview identity

def test_ai_preview_canonical_identity_is_stable_for_one_seventh(consumer_store):
    s = consumer_store
    plan = adjust_plan(s.employee, s.rational_kind, '-0.2')
    first = build_confirmation_preview(plan, session=s.db)['snapshot']['current']
    s.db.expire_all()
    second = build_confirmation_preview(plan, session=s.db)['snapshot']['current']
    assert first['current_balance_exact'] == second['current_balance_exact'] == canonical_amount(ONE_SEVENTH) == '1/7'
    assert first['current_balance'] == '1/7'


def test_ai_preview_identity_of_mixed_balance_matches_independent_fraction(consumer_store):
    s = consumer_store
    current = build_confirmation_preview(
        adjust_plan(s.employee, s.mixed_kind, '-0.2'), session=s.db)['snapshot']['current']
    assert current['current_balance_exact'] == canon(ONE_SEVENTH + Fraction(MIXED_FINITE))


# 2 -------------------------------------------------------------- stale confirmation

def test_stale_confirmation_accepts_scale_only_change(consumer_store):
    s = consumer_store
    db = s.db
    plan = confirmed(db, adjust_plan(s.employee, s.manual_kind, '-0.2'))
    current = plan['_confirmation_snapshot']['current']
    shown = current['current_balance']
    current['current_balance'] = f'{shown}0000' if '.' in str(shown) else f'{shown}.0000'  # same value, other scale
    assert str(current['current_balance']) != str(shown)
    count = ledger_count(db, s.employee, s.manual_kind)
    assert execute_plan(plan, 'synthetic', db=db).key == 'adjust_leave_balance'
    assert ledger_count(db, s.employee, s.manual_kind) == count + 1



def test_stale_confirmation_rejects_numeric_change_of_finite_balance(consumer_store):
    s = consumer_store
    db = s.db
    plan = confirmed(db, adjust_plan(s.employee, s.manual_kind, '-0.2'))
    db.add(finite_row(s.employee, s.manual_kind, Decimal('0.01')))
    db.flush()
    count = ledger_count(db, s.employee, s.manual_kind)
    with pytest.raises(ValueError, match=STALE):
        execute_plan(plan, 'synthetic', db=db)
    assert ledger_count(db, s.employee, s.manual_kind) == count


def test_stale_confirmation_rejects_numeric_change_of_rational_balance(consumer_store):
    s = consumer_store
    db = s.db
    plan = confirmed(db, adjust_plan(s.employee, s.rational_kind, '-0.2'))
    db.add(rational_row(s.employee, s.rational_kind, ONE_SEVENTH))  # 1/7 -> 2/7
    db.flush()
    assert canonical_amount(leave_balance(db, s.employee.id, s.rational_kind.id)) == '2/7'
    count = ledger_count(db, s.employee, s.rational_kind)
    with pytest.raises(ValueError, match=STALE):
        execute_plan(plan, 'synthetic', db=db)
    assert ledger_count(db, s.employee, s.rational_kind) == count


# 3 -------------------------------------------------------------- manual finite vs rational

def test_manual_finite_adjustment_against_rational_balance(consumer_store):
    s = consumer_store
    db = s.db
    plan = confirmed(db, adjust_plan(s.employee, s.rational_kind, '-0.2'))
    preview = build_confirmation_preview(plan, session=db)
    assert Fraction(preview['new_values']['resulting_balance']) == Fraction(-2, 35)
    result = execute_plan(plan, 'synthetic', db=db)
    assert result.params['days'] == Decimal('-0.2')
    assert canonical_amount(leave_balance(db, s.employee.id, s.rational_kind.id)) == '-2/35'
    assert exact_ledger_balance(db, s.employee.id, s.rational_kind.id) == Fraction(-2, 35)
    rows = db.scalars(select(LeaveBalanceTransaction).where(
        LeaveBalanceTransaction.employee_id == s.employee.id,
        LeaveBalanceTransaction.leave_type_id == s.rational_kind.id
    ).order_by(LeaveBalanceTransaction.id)).all()
    assert len(rows) == 2
    assert rows[0].days_delta is None and rows[0].exact_numerator == Decimal(1) and rows[0].exact_denominator == Decimal(7)
    assert rows[1].days_delta == Decimal('-0.2')            # finite lane stays finite
    assert rows[1].exact_numerator is None and rows[1].exact_denominator is None


def test_manual_high_scale_adjustment_against_rational_balance(consumer_store):
    s = consumer_store
    db = s.db
    delta = Decimal('-1.123456789123456789123456789123456789')
    expected = ONE_SEVENTH + Fraction(delta)
    plan = confirmed(db, adjust_plan(s.employee, s.rational_kind, str(delta)))
    assert Fraction(build_confirmation_preview(plan, session=db)['new_values']['resulting_balance']) == expected
    execute_plan(plan, 'synthetic', db=db)
    assert canonical_amount(leave_balance(db, s.employee.id, s.rational_kind.id)) == canon(expected)
    entry = db.scalars(select(LeaveBalanceTransaction).where(
        LeaveBalanceTransaction.employee_id == s.employee.id,
        LeaveBalanceTransaction.leave_type_id == s.rational_kind.id
    ).order_by(LeaveBalanceTransaction.id.desc())).first()
    db.refresh(entry)  # read the scale back from PostgreSQL, not the Python object
    assert format(entry.days_delta, 'f') == format(delta, 'f')
    assert entry.exact_numerator is None and entry.exact_denominator is None


# 4 -------------------------------------------------------------- workbook source

def _source(s, **overrides):
    args = dict(department_id=s.employee.department_id, employee_ids=(s.employee.id,),
                window_start=date(2026, 1, 1), month_start=date(2026, 1, 1),
                policy_digest='0' * 64, expected_epoch=s.auth['expected_epoch'])
    return read_workbook_source(s.db, **{**args, **overrides})


def test_workbook_source_shows_exact_fraction_and_keeps_legacy_finite_cast(consumer_store):
    s = consumer_store
    facts = _source(s)
    assert facts.digest == sha256(facts.canonical_json.encode()).hexdigest()
    ledger = {row['id']: row for row in json.loads(facts.canonical_json)['ledger']}
    rational = ledger[s.rational_row.id]
    assert rational['days_delta'] == '1/7'
    assert Decimal(rational['exact_numerator']) == 1 and Decimal(rational['exact_denominator']) == 7
    for row_id, amount in ((s.manual_row.id, HIGH_SCALE_MANUAL), (s.mixed_finite_row.id, MIXED_FINITE),
                           (s.plain_row.id, PLAIN_FINITE)):
        legacy = ledger[row_id]    # legacy finite rows: exact_* keys absent, PG numeric text untouched
        assert 'exact_numerator' not in legacy and 'exact_denominator' not in legacy
        assert legacy['days_delta'] == format(amount, 'f')
    assert ledger[s.mixed_rational_row.id]['days_delta'] == '1/7'


def test_workbook_source_digest_is_stable_and_numerically_sensitive(consumer_store):
    s = consumer_store
    first, again = _source(s), _source(s)
    assert first.digest == again.digest
    s.db.add(rational_row(s.employee, s.rational_kind, ONE_SEVENTH))   # 1/7 -> 2/7
    s.db.flush()
    changed = _source(s)
    assert changed.digest != first.digest
    amounts = [r['days_delta'] for r in json.loads(changed.canonical_json)['ledger']
               if r['leave_type_id'] == s.rational_kind.id]
    assert amounts == ['1/7', '1/7']


def test_workbook_source_refuses_stale_epoch(consumer_store):
    with pytest.raises(Exception):  # noqa: B017  (HTTPException 409 wrapped as ValueError by the bound wrapper)
        _source(consumer_store, expected_epoch='stale-epoch')


# 5 -------------------------------------------------------------- reconciliation

def test_reconciliation_sees_rational_and_mixed_ledger_total_without_writing(history_context):  # noqa: F811
    db, e, args = setup_source(history_context)
    from app.models import LeaveType
    annual = db.scalar(select(LeaveType).where(LeaveType.code == 'annual'))
    db.add(rational_row(e, annual, ONE_SEVENTH))
    db.flush()
    before = [db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction, AuditLog, LeaveRegisterEntry)]
    first = reconcile_selected_opening(db, release_evaluated_on=date(2026, 2, 28), **args)
    row = next(x for x in first.rows if x.code == 'A')
    assert row.ledger_total_separate == ONE_SEVENTH and isinstance(row.ledger_total_separate, Fraction)
    assert 'ledger_overlap_not_reconciled' in row.issues
    assert not first.posting_allowed and not first.establishes_spendable_credit
    db.add(finite_row(e, annual, Decimal('-1.5')))
    db.flush()
    second = reconcile_selected_opening(db, release_evaluated_on=date(2026, 2, 28), **args)
    row2 = next(x for x in second.rows if x.code == 'A')
    assert row2.ledger_total_separate == Fraction(-19, 14)
    assert len(first.reconciliation_digest) == 64 and first.reconciliation_digest != second.reconciliation_digest
    after = [db.scalar(select(func.count()).select_from(m)) for m in (LeaveBalanceTransaction, AuditLog, LeaveRegisterEntry)]
    assert after == [before[0] + 1, before[1], before[2]]   # only my own finite test row


# 6 -------------------------------------------------------------- overlap evidence

_RECORD = {'id': 1, 'employee_id': 1, 'leave_type_id': 1, 'start_date': date(2026, 1, 5),
           'end_date': date(2026, 1, 5), 'calendar_days': 1, 'charged_days': Decimal(1), 'status': 'confirmed'}
_REGISTER = [{'id': 1, 'leave_record_id': 1, 'employee_id': 1, 'work_date': date(2026, 1, 5), 'code': 'A'}]


@pytest.mark.parametrize('amount', [Decimal(-1), Fraction(-1, 7)])
def test_overlap_classifier_accepts_finite_and_rational_ledger_amounts(amount):
    ledger = [{'id': 1, 'leave_record_id': 1, 'employee_id': 1, 'leave_type_id': 1,
               'transaction_type': 'leave_use', 'days_delta': amount}]
    result = classify_record_overlap(
        record=_RECORD, ledger=ledger, register=_REGISTER, basis=date(2026, 1, 1),
        through=date(2026, 1, 31), type_codes={1: 'A'}, tracked_types={1: True})
    assert len(result.evidence_sha256) == 64 and result.netting_allowed is False
    assert ('linked_use_amount_or_count_disagrees' in result.issues) == (amount != Decimal(-1))


def test_overlap_reader_over_real_rational_leave_use_row(consumer_store):
    s = consumer_store
    db = s.db
    day = date(2026, 1, 5)
    record = create_leave_draft(db, employee_id=s.employee.id, leave_type_id=s.annual_kind.id,
                                start_date=day, end_date=day, charged_days=Decimal(1),
                                reason='Synthetic overlap record', actor='synthetic')
    record.status = 'confirmed'
    record.confirmed_by = 'synthetic'
    record.confirmed_at = utcnow()
    use = rational_row(s.employee, s.annual_kind, Fraction(-1, 7), transaction_type='leave_use',
                       leave_record_id=record.id)
    db.add(use)
    db.flush()
    mine = [c for c in read_overlap_candidates(db, employee_ids=(s.employee.id,),
                                               basis=date(2026, 1, 1), through=date(2026, 1, 31))
            if c.leave_record_id == record.id]
    assert len(mine) == 1
    assert mine[0].ledger_ids == (use.id,)
    assert 'linked_use_amount_or_count_disagrees' in mine[0].issues   # -1/7 is not -1
    assert mine[0].netting_allowed is False and len(mine[0].evidence_sha256) == 64


# 7 -------------------------------------------------------------- Excel export

def test_excel_export_keeps_exact_text_and_never_a_float(consumer_store):
    s = consumer_store
    s.db.add(rational_row(s.employee, s.rational_kind, Fraction(-1, 3)))
    s.db.flush()
    data = build_hr_workbook(s.db, date(2000, 1, 1), date(2100, 1, 1), employee_id=s.employee.id, language='en')
    sheet = load_workbook(BytesIO(data))['Leave balances']
    headers = [cell.value for cell in sheet[1]]
    code_i, type_i, delta_i = (headers.index(n) for n in ('Employee code', 'Leave type ID', 'Days delta'))
    got = [(r[type_i], r[delta_i]) for r in sheet.iter_rows(min_row=2, values_only=True)
           if r[code_i] == s.employee.code]
    assert sorted(got) == sorted([
        (s.rational_kind.id, '1/7'), (s.rational_kind.id, '-1/3'),
        (s.manual_kind.id, format(HIGH_SCALE_MANUAL, 'f')),
        (s.mixed_kind.id, '1/7'), (s.mixed_kind.id, format(MIXED_FINITE, 'f')),
        (s.plain_kind.id, '2.50'),
    ])
    assert all(isinstance(value, str) for _, value in got)     # never float/int
    assert not any('0.14285' in value or '0.33333' in value for _, value in got)


# 8 -------------------------------------------------------------- cross-type grouping

def test_employee_cross_type_grouping_keeps_nontracking_types(consumer_store):
    s = consumer_store
    e = s.employee.id
    totals = grouped_ledger_balances(s.db, employee_ids=[e])
    assert s.plain_kind.tracks_balance is False
    assert totals[(e, s.plain_kind.id)] == PLAIN_FINITE
    assert totals[(e, s.rational_kind.id)] == ONE_SEVENTH
    assert totals[(e, s.manual_kind.id)] == HIGH_SCALE_MANUAL
    mixed = ONE_SEVENTH + Fraction(MIXED_FINITE)
    assert Fraction(totals[(e, s.mixed_kind.id)]) == mixed
    expected = ONE_SEVENTH + Fraction(HIGH_SCALE_MANUAL) + mixed + Fraction(PLAIN_FINITE)
    assert canonical_amount(exact_sum(totals.values())) == canon(expected)
