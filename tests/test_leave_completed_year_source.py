from datetime import date
import pytest
from app.leave_completed_year_source import review_completed_year_source
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_month_earning import monthly  # noqa


def test_current_year_cannot_be_marked_complete_with_future_review(monthly):
    db, employee, kinds, args = monthly
    selection = args(date(2026, 2, 1), date(2026, 2, 28))
    identity = {k: selection[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    with pytest.raises(ValueError, match='Actual completed'):
        review_completed_year_source(db, employee_id=employee.id, leave_type_id=kinds['W'],
            source_year=2026, selection_id=selection['selection_id'], reviewed_on=date(2027, 1, 1),
            current_insurance_years=selection['current_insurance_years'],
            age_evaluation_dates=selection['age_evaluation_dates'], **identity)


@pytest.fixture
def prior_year(history_context, request):
    from datetime import timedelta
    from decimal import Decimal
    from sqlalchemy import select
    from app.models import Employee, EmployeeServicePeriod, LeaveRegisterEntry, LeaveType
    from app.leave_policy_preview import LeavePolicy
    from app.leave_policy_store import persist_policy_version
    from app.leave_manual_opening_groups import persist_manual_opening_group
    from app.leave_opening_source import review_opening_source, confirm_opening_source
    from app.leave_opening_apply import review_opening_apply, apply_confirmed_opening
    from app.leave_month_earning import review_month_earning, apply_month_earning
    db, actor, auth, fact = history_context
    options = getattr(request, 'param', {})
    employee = db.get(Employee, fact.employee_id); employee.hire_date = options.get('hire_date', date(2025, 11, 25))
    db.add(EmployeeServicePeriod(employee_id=employee.id, start_date=employee.hire_date))
    policy = persist_policy_version(db, policy=LeavePolicy(False, False, True, False, 'calendar', 'prorated', False, (90, 180)), reason='Synthetic explicit', **auth)
    group, _ = persist_manual_opening_group(db, employee_id=employee.id,
        balances={c: Decimal(0) for c in ('A', 'W', 'H')}, effective_start=employee.hire_date,
        reason='Synthetic source-year baseline', **auth)
    reviewed = review_opening_source(db, group_id=group.id, policy_id=policy.id, **auth)
    selected = confirm_opening_source(db, group_id=group.id, policy_id=policy.id,
        expected_previous_id=None, reviewed_source_sha256=reviewed['source_sha256'],
        human_confirmed=True, reason='Synthetic confirm', **auth)
    kinds = {c: db.scalar(select(LeaveType.id).where(LeaveType.code == host))
        for c, host in {'A': 'annual', 'W': 'weekly_rest', 'H': 'official_holiday'}.items()}
    reviewed = review_opening_apply(db, selection_id=selected.id, type_map=kinds, **auth)
    apply_confirmed_opening(db, selection_id=selected.id, type_map=kinds,
        reviewed_apply_sha256=reviewed['apply_sha256'], human_confirmed=True, reason='Synthetic opening', **auth)
    day = employee.hire_date
    while day <= date(2025, 12, 31):
        code = 'A' if day.month == 1 and day.day <= options.get('used_annual', 0) else ''
        db.add(LeaveRegisterEntry(employee_id=employee.id, work_date=day, code=code, source='synthetic', recorded_by='synthetic'))
        day += timedelta(days=1)
    db.flush()
    from calendar import monthrange
    for month in range(employee.hire_date.month, 13):
        evaluated = date(2026, 1, 1) if month == 12 else date(2025, month, monthrange(2025, month)[1])
        operation = format(month, '064x')
        months = [date(2025, m, 1) for m in range(1, month + 1)]
        call = dict(selection_id=selected.id, month_start=date(2025, month, 1), evaluated_on=evaluated,
            current_insurance_years={(employee.id, m): None for m in months}, age_evaluation_dates={m: m for m in months}, **auth)
        review = review_month_earning(db, **call)
        apply_month_earning(db, operation_id=operation, reviewed_digest=review['reviewed_digest'],
            human_confirmed=True, reason='Synthetic actual earned source', **call)
    return db, employee, kinds, dict(employee_id=employee.id, leave_type_id=kinds['W'], source_year=2025,
        selection_id=selected.id, reviewed_on=date(2026, 1, 1),
        current_insurance_years=call['current_insurance_years'], age_evaluation_dates=call['age_evaluation_dates'], **auth)


def test_actual_prior_year_receipts_complete_and_current_sources_verify(prior_year):
    from fractions import Fraction
    from sqlalchemy import select, func
    from app.models import LeaveBalanceTransaction
    db, employee, kinds, args = prior_year
    before = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    result = review_completed_year_source(db, **args)
    assert result['source_verified'] and not result['posting_allowed']
    assert result['amount'] == Fraction(37, 7)
    assert result['source']['covered_months'] == ['2025-11-01', '2025-12-01']
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == before


def test_changed_final_month_register_invalidates_prior_year_proof(prior_year):
    from sqlalchemy import select
    from app.models import LeaveRegisterEntry
    db, employee, kinds, args = prior_year
    row = db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id == employee.id,
        LeaveRegisterEntry.work_date == date(2025, 12, 31)))
    row.code = 'U'; db.flush()
    with pytest.raises(ValueError, match='Prior earned source changed'):
        review_completed_year_source(db, **args)


@pytest.mark.parametrize('change', ['service', 'role', 'epoch', 'missing_month', 'custom_counter'])
def test_closed_year_source_rechecks_coverage_and_current_authority(prior_year, change):
    from sqlalchemy import select, text
    from app.models import AdminAccount, EmployeeServicePeriod, LeaveMonthEarning, LeaveType
    db, employee, kinds, args = prior_year
    if change == 'service':
        row = db.scalar(select(EmployeeServicePeriod).where(EmployeeServicePeriod.employee_id == employee.id))
        row.end_date = date(2025, 11, 30); db.flush()
    elif change == 'role':
        db.get(AdminAccount, args['actor_id']).role = 'assistant_manager'; db.flush()
    elif change == 'epoch':
        args = {**args, 'expected_epoch': 'different-restore-epoch'}
    elif change == 'missing_month':
        db.execute(text('ALTER TABLE leave_month_earnings DISABLE TRIGGER USER'))
        db.execute(text("DELETE FROM leave_month_earnings WHERE employee_id=:employee AND month_start='2025-12-01'"), {'employee': employee.id})
        db.execute(text('ALTER TABLE leave_month_earnings ENABLE TRIGGER USER'))
    elif change == 'custom_counter':
        kind = LeaveType(code='synthetic_counter', name_ar='Synthetic counter', name_en='Synthetic counter', active=True, tracks_balance=False)
        db.add(kind); db.flush(); args = {**args, 'leave_type_id': kind.id}
    with pytest.raises(ValueError):
        review_completed_year_source(db, **args)


@pytest.mark.parametrize('category', ['A', 'H'])
def test_prior_year_annual_and_holiday_source_are_separate_exact_amounts(prior_year, category):
    from fractions import Fraction
    db, employee, kinds, args = prior_year
    result = review_completed_year_source(db, **{**args, 'leave_type_id': kinds[category]})
    assert result['source_verified'] and not result['posting_allowed']
    assert result['amount'] >= Fraction(0) and result['held_annual'] >= Fraction(0)
    if category == 'H':
        assert result['amount'] == 0 and result['held_annual'] == 0
