"""Real-PG proof: after an epoch bump the old opening chain is unreadable, a fresh HR-reviewed
rebind restores it without a second credit, and tampering or mismatches refuse."""
import json
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.leave_manual_opening_groups import persist_manual_opening_group
from app.leave_month_earning import apply_month_earning, review_month_earning
from app.leave_opening_apply import apply_confirmed_opening, review_opening_apply
from app.leave_opening_rebind import apply_opening_rebind, lineage, review_opening_rebind
from app.leave_opening_receipt import verified_manual_opening
from app.leave_opening_source import confirm_opening_source, review_opening_source
from app.leave_policy_preview import LeavePolicy
from app.leave_policy_store import persist_policy_version
from app.leave_workflow import leave_balance
from app.models import (AdminSession, AuditLog, Employee, EmployeeServicePeriod, LeaveBalanceTransaction,
                        LeaveHistoricalEvidencePreparation, LeaveMonthEarning, LeaveOpeningSelection,
                        LeaveRegisterEntry, LeaveType, SystemSetting)
from app.restore_lock import RESTORE_EPOCH_KEY, read_restore_epoch
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401

POLICY = LeavePolicy(False, False, True, False, 'calendar', 'prorated', False, (90, 180))


def confirm(db, employee, auth, balances=None, start=None, policy=POLICY, reason='Synthetic baseline'):
    pol = persist_policy_version(db, policy=policy, reason='Synthetic explicit', **auth)
    group, _ = persist_manual_opening_group(db, employee_id=employee.id,
        balances=balances or {c: Decimal(0) for c in 'AWH'}, effective_start=start or employee.hire_date, reason=reason, **auth)
    review = review_opening_source(db, group_id=group.id, policy_id=pol.id, **auth)
    prev = review['expected_previous_id']
    return confirm_opening_source(db, group_id=group.id, policy_id=pol.id, expected_previous_id=prev,
        reviewed_source_sha256=review['source_sha256'], reason='Synthetic confirm', human_confirmed=True, **auth)


def restore(db, auth, epoch):
    """What _finalize_restored_security does to the facts these proofs read: epoch bump, sessions replaced."""
    db.merge(SystemSetting(key=RESTORE_EPOCH_KEY, value=epoch, updated_by='synthetic-restore'))
    db.flush()
    session = AdminSession(id='synthetic-after-restore-' + epoch, admin_account_id=auth['actor_id'])
    db.add(session); db.flush()
    assert read_restore_epoch(db) == epoch
    return {**auth, 'session_id': session.id, 'expected_epoch': epoch}


@pytest.fixture
def world(history_context):  # noqa: F811
    db, _actor, auth, fact = history_context
    employee = db.get(Employee, fact.employee_id); employee.hire_date = date(2026, 2, 25)
    db.add(EmployeeServicePeriod(employee_id=employee.id, start_date=employee.hire_date))
    selected = confirm(db, employee, auth, {'A': Decimal('3.5'), 'W': Decimal(1), 'H': Decimal(0)})
    kinds = {c: db.scalar(select(LeaveType.id).where(LeaveType.code == host)) for c, host in
             {'A': 'annual', 'W': 'weekly_rest', 'H': 'official_holiday'}.items()}
    opening = review_opening_apply(db, selection_id=selected.id, type_map=kinds, **auth)
    apply_confirmed_opening(db, selection_id=selected.id, type_map=kinds, reviewed_apply_sha256=opening['apply_sha256'],
        human_confirmed=True, reason='Synthetic opening', **auth)
    for n in range(35):
        day = employee.hire_date + timedelta(days=n)
        db.add(LeaveRegisterEntry(employee_id=employee.id, work_date=day, code='', source='reviewed-return', recorded_by='synthetic'))
    db.flush()

    def args(sel, auth_, month, evaluated):
        months = [date(2026, m, 1) for m in range(1, month.month + 1)]
        return dict(selection_id=sel, month_start=month, evaluated_on=evaluated,
            current_insurance_years={(employee.id, m): None for m in months},
            age_evaluation_dates={m: m for m in months}, **auth_)
    yield db, employee, kinds, selected, auth, args


def post(db, a, op):
    review = review_month_earning(db, **a)
    return apply_month_earning(db, operation_id=op, reviewed_digest=review['reviewed_digest'], human_confirmed=True,
        reason='Synthetic reviewed earning', **a)


def rebind(db, old, new, auth, reason='Synthetic rebind'):
    r = review_opening_rebind(db, old_selection_id=old, new_selection_id=new, **auth)
    return r, apply_opening_rebind(db, old_selection_id=old, new_selection_id=new,
        reviewed_rebind_sha256=r['rebind_sha256'], reason=reason, human_confirmed=True, **auth)


def ledger_count(db):
    return db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))


def test_old_chain_unreadable_then_rebind_restores_without_second_credit(world):
    db, e, kinds, old, auth, args = world
    feb = post(db, args(old.id, auth, date(2026, 2, 1), date(2026, 2, 28)), 'a' * 64)
    old_hist = db.scalar(select(func.count()).select_from(LeaveHistoricalEvidencePreparation))
    auth2 = restore(db, auth, 'E1-restored')
    with pytest.raises(ValueError):  # old selection is epoch-bound
        review_month_earning(db, **args(old.id, auth2, date(2026, 3, 1), date(2026, 4, 1)))
    with pytest.raises(ValueError):
        verified_manual_opening(db, selection_id=old.id, **auth2)
    new = confirm(db, e, auth2, {'A': Decimal('3.5'), 'W': Decimal(1), 'H': Decimal(0)})
    assert new.id != old.id and new.restore_epoch == 'E1-restored'
    assert db.scalar(select(func.count()).select_from(LeaveHistoricalEvidencePreparation)) == old_hist + 3  # re-prepared, old rows kept
    # new selection cannot post a second opening and cannot verify before a rebind
    with pytest.raises(ValueError):
        verified_manual_opening(db, selection_id=new.id, **auth2)
    opening2 = review_opening_apply(db, selection_id=new.id, type_map=kinds, **auth2) if False else None
    with pytest.raises(ValueError, match='Existing opening receipt'):
        o = review_opening_apply(db, selection_id=new.id, type_map=kinds, **auth2)
    ledgers = ledger_count(db); audits = db.scalar(select(func.count()).select_from(AuditLog))
    review, receipt = rebind(db, old.id, new.id, auth2)
    assert receipt['replayed'] is False and review['ledger_applied'] is False
    assert ledger_count(db) == ledgers and db.scalar(select(func.count()).select_from(AuditLog)) == audits + 1
    # idempotent replay, wrong digest after the fact refuses
    again = apply_opening_rebind(db, old_selection_id=old.id, new_selection_id=new.id,
        reviewed_rebind_sha256=review['rebind_sha256'], reason='Synthetic rebind', human_confirmed=True, **auth2)
    assert again['replayed'] is True and db.scalar(select(func.count()).select_from(AuditLog)) == audits + 1
    with pytest.raises(ValueError, match='different reviewed evidence'):
        apply_opening_rebind(db, old_selection_id=old.id, new_selection_id=new.id,
            reviewed_rebind_sha256='f' * 64, reason='Synthetic rebind', human_confirmed=True, **auth2)
    v = verified_manual_opening(db, selection_id=new.id, **auth2)
    assert v['root_selection_id'] == old.id and v['amounts']['A'] == Decimal('3.5')
    assert lineage(db, new.id)[0] == [new.id, old.id]
    # next month: prior Feb receipt (old selection) accepted through lineage; held annual released once
    mar = post(db, args(new.id, auth2, date(2026, 3, 1), date(2026, 4, 1)), 'b' * 64)
    assert {r['earning_month'] for r in mar['ledger'] if r['category'] == 'A'} == {'2026-02-01', '2026-03-01'}
    assert leave_balance(db, e.id, kinds['A']) > 0
    openings = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction).where(LeaveBalanceTransaction.transaction_type == 'opening', LeaveBalanceTransaction.employee_id == e.id))
    assert openings == 2  # A 3.5 and W 1, original only
    # a month receipt created under the old selection is untouched
    assert db.get(LeaveMonthEarning, 'a' * 64).selection_id == old.id


def prepared(world, epoch='E1-restored', balances=None, policy=POLICY, start=None):
    db, e, kinds, old, auth, args = world
    auth2 = restore(db, auth, epoch)
    new = confirm(db, e, auth2, balances or {'A': Decimal('3.5'), 'W': Decimal(1), 'H': Decimal(0)}, start=start, policy=policy)
    return db, e, old, new, auth2


@pytest.mark.parametrize('case', ['amount', 'policy', 'date', 'employee'])
def test_mismatched_fresh_source_refuses(world, case):
    db, e, kinds, old, auth, args = world
    bal = {'A': Decimal('3.5'), 'W': Decimal(1), 'H': Decimal(0)}
    kw = {}
    if case == 'amount': bal['A'] = Decimal('4')
    if case == 'policy': kw['policy'] = LeavePolicy(True, False, True, False, 'calendar', 'prorated', False, (90, 180))
    if case == 'date': kw['start'] = e.hire_date + timedelta(days=1)
    if case == 'employee':
        other = Employee(code='SYN-OTHER', full_name='Synthetic other', department_id=e.department_id, position='S',
            hire_date=date(2026, 2, 25), status='active', basic_salary=Decimal(1), daily_hours=Decimal(8))
        db.add(other); db.flush()
    auth2 = restore(db, auth, 'E1-restored')
    from tests.test_restore_epoch_rebind import confirm as c
    new = c(db, other if case == 'employee' else e, auth2, bal, **kw)
    with pytest.raises(ValueError):
        review_opening_rebind(db, old_selection_id=old.id, new_selection_id=new.id, **auth2)
    n = db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == 'leave_opening_rebound'))
    assert n == 0


def test_authority_and_same_epoch_and_second_successor_refuse(world):
    db, e, kinds, old, auth, args = world
    # no restore: current selection is not a rebind target
    with pytest.raises(ValueError):
        review_opening_rebind(db, old_selection_id=old.id, new_selection_id=old.id, **auth)
    db, e, old, new, auth2 = prepared(world)
    r = review_opening_rebind(db, old_selection_id=old.id, new_selection_id=new.id, **auth2)
    for bad in ({'human_confirmed': False, 'reason': 'x'}, {'human_confirmed': True, 'reason': ' '}):
        with pytest.raises(ValueError):
            apply_opening_rebind(db, old_selection_id=old.id, new_selection_id=new.id, reviewed_rebind_sha256=r['rebind_sha256'], **bad, **auth2)
    # stale epoch, inactive actor, wrong role are refused by the existing exclusive guard
    with pytest.raises(Exception):
        apply_opening_rebind(db, old_selection_id=old.id, new_selection_id=new.id, reviewed_rebind_sha256=r['rebind_sha256'],
            reason='x', human_confirmed=True, **{**auth2, 'expected_epoch': 'older'})
    actor = db.get(__import__('app.models', fromlist=['AdminAccount']).AdminAccount, auth2['actor_id'])
    actor.role = 'finance'; db.flush()
    with pytest.raises(ValueError):
        apply_opening_rebind(db, old_selection_id=old.id, new_selection_id=new.id, reviewed_rebind_sha256=r['rebind_sha256'],
            reason='x', human_confirmed=True, **auth2)
    actor.role = 'system_admin'; db.flush()
    rebind(db, old.id, new.id, auth2)
    # second restore, second rebind chains; a different successor for the old id refuses
    auth3 = restore(db, auth2, 'E2-restored')
    new3 = confirm(db, e, auth3, {'A': Decimal('3.5'), 'W': Decimal(1), 'H': Decimal(0)})
    r3, _ = rebind(db, new.id, new3.id, auth3)
    assert lineage(db, new3.id)[0] == [new3.id, new.id, old.id]
    assert verified_manual_opening(db, selection_id=new3.id, **auth3)['root_selection_id'] == old.id
    with pytest.raises(ValueError):
        verified_manual_opening(db, selection_id=new.id, **auth3)  # superseded epoch head


@pytest.mark.parametrize('tamper', ['ledger_amount', 'receipt_amount', 'rebind_json', 'rebind_old', 'rebind_dup', 'ledger_key'])
def test_tampered_archive_or_receipt_fails_closed(world, tamper):
    db, e, old, new, auth2 = prepared(world)
    rebind(db, old.id, new.id, auth2)
    assert verified_manual_opening(db, selection_id=new.id, **auth2)
    row = db.scalar(select(AuditLog).where(AuditLog.action == 'leave_opening_rebound'))
    ledger = db.scalars(select(LeaveBalanceTransaction).where(LeaveBalanceTransaction.source_operation_key.like('manual_opening:%'))).first()
    receipt = db.scalar(select(AuditLog).where(AuditLog.action == 'leave_opening_applied')) or db.scalar(select(AuditLog).where(AuditLog.entity_id == old.id, AuditLog.entity_type == 'leave_opening_selection', AuditLog.action != 'leave_opening_rebound'))
    from sqlalchemy import text
    with db.no_autoflush:
        if tamper == 'ledger_amount':
            db.execute(text('ALTER TABLE leave_balance_transactions DISABLE TRIGGER USER'))
            db.execute(text("UPDATE leave_balance_transactions SET days_delta = days_delta + 1 WHERE id = :i"), {'i': ledger.id})
        elif tamper == 'ledger_key':
            db.execute(text('ALTER TABLE leave_balance_transactions DISABLE TRIGGER USER'))
            db.execute(text(r"UPDATE leave_balance_transactions SET source_operation_key = 'manual_opening:' || repeat('a',64) || '\:A' WHERE id = :i"), {'i': ledger.id})
        elif tamper == 'receipt_amount':
            db.execute(text('ALTER TABLE audit_logs DISABLE TRIGGER USER'))
            body = json.loads(receipt.new_value); body['amounts']['A'] = '9'
            db.execute(text('UPDATE audit_logs SET new_value = :v WHERE id = :i'), {'v': json.dumps(body), 'i': receipt.id})
        elif tamper == 'rebind_json':
            db.execute(text('ALTER TABLE audit_logs DISABLE TRIGGER USER'))
            db.execute(text("UPDATE audit_logs SET new_value = '{bad' WHERE id = :i"), {'i': row.id})
        elif tamper == 'rebind_old':
            db.execute(text('ALTER TABLE audit_logs DISABLE TRIGGER USER'))
            body = json.loads(row.new_value); body['old_apply_sha256'] = 'e' * 64
            db.execute(text('UPDATE audit_logs SET new_value = :v WHERE id = :i'), {'v': json.dumps(body), 'i': row.id})
        elif tamper == 'rebind_dup':
            db.add(AuditLog(actor='x', action='leave_opening_rebound', entity_type='leave_opening_selection', entity_id=new.id,
                old_value=old.id, new_value=row.new_value, reason='dup')); db.flush()
    db.expire_all()
    with pytest.raises(ValueError):
        verified_manual_opening(db, selection_id=new.id, **auth2)


def test_cash_out_before_rebind_refuses_after_rebind_debits_once_from_original_ledger(world):
    from fractions import Fraction

    from app.leave_cash_out import apply_cash_out, review_cash_out
    from tests.test_leave_cash_out import payroll
    db, e, kinds, old, auth, args = world
    post(db, args(old.id, auth, date(2026, 2, 1), date(2026, 2, 28)), 'a' * 64)
    post(db, args(old.id, auth, date(2026, 3, 1), date(2026, 4, 1)), 'b' * 64)
    _run, item = payroll(db, e)
    auth2 = restore(db, auth, 'E1-restored')
    cargs = dict(employee_id=e.id, leave_type_id=kinds['A'], days='1', payroll_item_id=item.id,
                 evaluated_on=date(2026, 4, 2), **auth2)
    with pytest.raises(ValueError):  # old opening is epoch-bound: fail closed, no stale authority
        review_cash_out(db, **cargs)
    new = confirm(db, e, auth2, {'A': Decimal('3.5'), 'W': Decimal(1), 'H': Decimal(0)})
    with pytest.raises(ValueError):  # a fresh selection alone is not proof
        review_cash_out(db, **cargs)
    rebind(db, old.id, new.id, auth2)
    before = leave_balance(db, e.id, kinds['A'])
    review = review_cash_out(db, **cargs)
    result = apply_cash_out(db, operation_id='d' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True,
        reason='Synthetic cash', **cargs)
    assert result['cash_egp'] == '120.00'
    assert leave_balance(db, e.id, kinds['A']) == before - 1
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction).where(
        LeaveBalanceTransaction.transaction_type == 'opening', LeaveBalanceTransaction.employee_id == e.id)) == 2


from tests.test_leave_completed_year_source import prior_year  # noqa: E402,F401
from tests.test_leave_next_year_earning import FULL, next_month  # noqa: E402


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_year_checkpoint_month_receipt_and_carry_consumption_follow_lineage_after_restore(prior_year):
    import json
    from fractions import Fraction

    from app.leave_carry_consumption import record_carry_consumption, review_carry_consumption
    from app.models import LeaveCarryAllocation, LeaveCarryConsumption, LeaveYearTransition
    db, call = next_month(prior_year)  # actual 2025 months + checkpoint recorded under epoch None
    employee = prior_year[1]
    old = call['selection_id']
    ident = ('actor_id', 'session_id', 'session_version', 'expected_epoch')
    auth = {k: call[k] for k in ident}
    jan = review_month_earning(db, **call)
    apply_month_earning(db, operation_id='b' * 64, reviewed_digest=jan['reviewed_digest'], human_confirmed=True,
        reason='Synthetic', **call)  # January receipt under the OLD selection
    checkpoints = db.scalar(select(func.count()).select_from(LeaveYearTransition))
    auth2 = restore(db, auth, 'E1-restored')
    grant = db.scalar(select(LeaveCarryAllocation).where(LeaveCarryAllocation.employee_id == employee.id,
        LeaveCarryAllocation.leave_type_id == prior_year[2]['W']))
    usage = next(i for i in json.loads(db.get(LeaveMonthEarning, 'b' * 64).result_json)['ledger']
                 if i['category'] == 'W' and i['kind'] == 'usage')
    cons = dict(grant_kind='carry', grant_id=grant.id, employee_id=employee.id, leave_type_id=prior_year[2]['W'],
        monthly_receipt_id='b' * 64, usage_ledger_id=usage['id'], work_date=date(2026, 1, 3), quantity=Fraction(1),
        reason='Synthetic selected grant', **auth2)
    with pytest.raises(ValueError):
        review_carry_consumption(db, **cons)  # old epoch-bound proof fails closed
    new = confirm(db, employee, auth2, {c: Decimal(0) for c in 'AWH'}, start=employee.hire_date)
    with pytest.raises(ValueError):
        review_carry_consumption(db, **cons)  # fresh selection alone is not proof
    rebind(db, old, new.id, auth2)
    review = review_carry_consumption(db, **cons)
    done = record_carry_consumption(db, operation_id='a' * 64, reviewed_digest=review['reviewed_digest'],
        human_confirmed=True, **cons)
    assert done['ledger_delta'] == '0/1'
    assert db.scalar(select(func.count()).select_from(LeaveCarryConsumption)) == 1
    assert db.scalar(select(func.count()).select_from(LeaveYearTransition)) == checkpoints  # no checkpoint rewritten
    # next month under the NEW selection: the old-epoch checkpoint and January receipt are accepted via lineage
    for d in range(1, 29):
        db.add(LeaveRegisterEntry(employee_id=employee.id, work_date=date(2026, 2, d), code='', source='synthetic', recorded_by='synthetic'))
    db.flush()
    feb = date(2026, 2, 1)
    fargs = dict(selection_id=new.id, month_start=feb, evaluated_on=date(2026, 3, 1),
        current_insurance_years={(employee.id, m): None for m in (date(2026, 1, 1), feb)},
        age_evaluation_dates={m: m for m in (date(2026, 1, 1), feb)}, **auth2)
    rev = review_month_earning(db, **fargs)
    assert rev['plan']['year_transitions']
    apply_month_earning(db, operation_id='c' * 64, reviewed_digest=rev['reviewed_digest'], human_confirmed=True,
        reason='Synthetic', **fargs)
    assert db.get(LeaveMonthEarning, 'c' * 64).selection_id == new.id
    assert db.get(LeaveMonthEarning, 'b' * 64).selection_id == old
    # a checkpoint whose recorded epoch does not belong to its selection refuses
    from sqlalchemy import text
    row = db.scalar(select(LeaveYearTransition))
    db.execute(text('ALTER TABLE leave_year_transitions DISABLE TRIGGER USER'))
    req = json.loads(row.request_json); req['expected_epoch'] = 'forged'
    db.execute(text('UPDATE leave_year_transitions SET request_json=:r WHERE id=:i'), {'r': json.dumps(req), 'i': row.id})
    db.expire_all()
    with pytest.raises(ValueError):
        mar = [date(2026, m, 1) for m in (1, 2, 3)]
        review_month_earning(db, **{**fargs, 'month_start': date(2026, 3, 1), 'evaluated_on': date(2026, 4, 1),
            'current_insurance_years': {(employee.id, m): None for m in mar}, 'age_evaluation_dates': {m: m for m in mar}})
