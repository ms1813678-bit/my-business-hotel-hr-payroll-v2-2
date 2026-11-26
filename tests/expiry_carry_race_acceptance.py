"""Real two-session PG races around an expired carry grant (scratch DB, fresh DB per case)."""
import pytest
from sqlalchemy import func, select

from app.db import SessionLocal, engine
from app.leave_carry_consumption import record_carry_consumption, review_carry_consumption
from app.leave_carry_expiry import apply_carry_expiry, review_carry_expiry
from app.models import LeaveBalanceTransaction, LeaveCarryConsumption, LeaveCarryExpiry
from tests.test_leave_carry_expiry import prepared_expiry  # noqa: F401
from tests.test_leave_next_year_earning import FULL
from tests.zero_carry_race_acceptance import run_pair
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401


def counts():
    with SessionLocal() as s:
        return tuple(s.scalar(select(func.count()).select_from(m)) for m in (LeaveCarryExpiry, LeaveCarryConsumption, LeaveBalanceTransaction))


def prep(prior_year):
    assert 'race' in engine.url.database
    db, call, usage, result = prepared_expiry(prior_year, 'W')
    ap = dict(operation_id='f' * 64, reviewed_digest=result['reviewed_digest'], human_confirmed=True, **call)
    base = counts_from(db)
    db.commit()
    return db, call, usage, ap, base


def counts_from(db):
    return tuple(db.scalar(select(func.count()).select_from(m)) for m in (LeaveCarryExpiry, LeaveCarryConsumption, LeaveBalanceTransaction))


P = pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)


@P
def test_two_sessions_same_expiry_operation_idempotent_one_debit(prior_year):
    db, call, usage, ap, base = prep(prior_year)
    f = lambda s: apply_carry_expiry(s, **ap)
    out = run_pair(f, f)
    assert out['a'][0] == 'ok' and out['b'][0] == 'ok' and out['a'][1] == out['b'][1], out
    assert counts() == (base[0] + 1, base[1], base[2] + 1), (counts(), base)


@P
def test_two_sessions_different_operation_ids_one_commit_other_refuses(prior_year):
    db, call, usage, ap, base = prep(prior_year)
    out = run_pair(lambda s: apply_carry_expiry(s, **ap), lambda s: apply_carry_expiry(s, **{**ap, 'operation_id': '9' * 64}))
    assert out['a'][0] == 'ok' and out['b'][:2] == ('err', 'ValueError'), out
    assert out['b_wait'] >= 1.0, out
    assert counts() == (base[0] + 1, base[1], base[2] + 1), (counts(), base)


@P
def test_consumption_replay_and_new_op_racing_expiry_never_changes_ledger_twice(prior_year):
    db, call, usage, ap, base = prep(prior_year)
    first = {**usage, 'quantity': usage['quantity']}
    def attempt(s):
        rv = review_carry_consumption(s, **first)
        return record_carry_consumption(s, operation_id='d' * 64, reviewed_digest=rv['reviewed_digest'], human_confirmed=True, **first)
    out = run_pair(lambda s: apply_carry_expiry(s, **ap), attempt)
    after = counts()
    # whatever B did (clean refusal or stored-result replay), exactly one expiry debit, no new consumption row
    assert out['a'][0] == 'ok' and out['b'][0] in ('ok', 'err'), out
    if out['b'][0] == 'err':
        assert out['b'][1] == 'ValueError', out
    assert after == (base[0] + 1, base[1], base[2] + 1), (after, base, out['b'])
    print('B outcome after expiry:', out['b'])
