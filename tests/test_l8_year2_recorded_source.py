# ruff: noqa: F811
"""Candidate tests for the isolated versioned later-year recorded source. Fixtures reused from the year-2 net tests."""
import copy
import json
from datetime import date
from fractions import Fraction

import pytest
from sqlalchemy import select

from app.leave_exact_amount import canonical_amount
from app.leave_month_earning import digest
from app.leave_year2_recorded_source import build_year2_recorded_source, verify_year2_recorded_source
from app.models import AuditLog, LeaveBalanceTransaction, LeaveCarryConsumption
from tests.test_l8_year2_net_source import (full_year, zero_year, FULL, prior_year, context, history_context)  # noqa

P = pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)


def seal(stored):
    stored['source_digest'] = digest(stored['source'])
    return stored


def check(db, args, stored):
    return verify_year2_recorded_source(db, stored=stored, employee_id=args['employee_id'], leave_type_id=args['leave_type_id'])


@P
def test_nonzero_9_over_7_attribution_round_trips_and_recomputes_from_named_rows(prior_year, monkeypatch):
    db, args = full_year(prior_year, monkeypatch)
    out = build_year2_recorded_source(db, **args)
    stored = json.loads(json.dumps(out['stored']))           # actual JSON round trip
    verified = check(db, args, stored)
    assert verified['attributed'] == Fraction(9, 7) and verified['consumptions'] == 2
    assert verified['net'] == verified['own_amount'] + Fraction(9, 7) == out['amount']
    assert not verified['posting_allowed'] and not out['posting_allowed'] and verified['semantic'] == 'read_only_source_not_available_balance'


@P
def test_zero_attribution_records_empty_consumptions_and_net_equals_own_rows(prior_year, monkeypatch):
    db, args = zero_year(prior_year, monkeypatch)
    out = build_year2_recorded_source(db, **args)
    verified = check(db, args, out['stored'])
    assert verified['consumptions'] == 0 and verified['net'] == verified['own_amount']


def _mutations(stored):
    c0 = lambda s: s['source']['consumptions'][0]                      # noqa: E731
    return {
        'digest': lambda s: s.__setitem__('source_digest', '0' * 64),
        'version': lambda s: s['source'].__setitem__('version', 'year2-recorded-v0'),
        'extra_key': lambda s: s['source'].__setitem__('x', 1),
        'net_amount': lambda s: s['source'].__setitem__('net_amount', '1/1'),
        'carried_amount': lambda s: s.__setitem__('carried_amount', '1/1'),
        'attributed_amount': lambda s: s['source'].__setitem__('attributed_amount', '1/7'),
        'quantity': lambda s: c0(s).__setitem__('quantity', '1/7'),
        'grant_year_not_prior': lambda s: c0(s).__setitem__('grant_year', 2026),
        'grant_year_wrong': lambda s: c0(s).__setitem__('grant_year', 2024),
        'grant_ceiling': lambda s: c0(s).__setitem__('grant_ceiling', '99/1'),
        'usage_amount': lambda s: c0(s).__setitem__('usage_amount', '99/1'),
        'usage_ledger_id': lambda s: c0(s).__setitem__('usage_ledger_id', 999999),
        'work_date': lambda s: c0(s).__setitem__('work_date', '2026-01-11'),
        'grant_id': lambda s: c0(s).__setitem__('grant_id', 'e' * 64),
        'result_digest': lambda s: c0(s).__setitem__('result_digest', '0' * 64),
        'duplicate_binding': lambda s: s['source']['consumptions'].append(copy.deepcopy(c0(s))),
        'dropped_binding': lambda s: s['source']['consumptions'].pop(),
        'extra_binding_key': lambda s: c0(s).__setitem__('extra', 1),
        'own_month_binding': lambda s: s['source']['own']['verified_months'][0].__setitem__(1, '0' * 64),
    }


@P
def test_every_recorded_field_tamper_refuses_even_with_a_resealed_digest(prior_year, monkeypatch):
    db, args = full_year(prior_year, monkeypatch)
    stored = json.loads(json.dumps(build_year2_recorded_source(db, **args)['stored']))
    check(db, args, stored)                                           # baseline passes
    muts = _mutations(stored)
    assert len(muts) >= 19
    for name, mutate in muts.items():                                 # verification is read-only: one fixture, many tampers
        bad = copy.deepcopy(stored)
        mutate(bad)
        if name != 'digest':
            seal(bad)
        with pytest.raises(ValueError):
            check(db, args, bad)
        check(db, args, stored)                                       # untampered proof still verifies after each refusal


@P
def test_forged_consumption_receipt_without_the_immutable_audit_proof_refuses(prior_year, monkeypatch):
    db, args = full_year(prior_year, monkeypatch)
    stored = json.loads(json.dumps(build_year2_recorded_source(db, **args)['stored']))
    real = db.get(LeaveCarryConsumption, stored['source']['consumptions'][0]['id'])
    forged = LeaveCarryConsumption(id='f' * 64, employee_id=real.employee_id, leave_type_id=real.leave_type_id,
        grant_kind=real.grant_kind, grant_id=real.grant_id, usage_ledger_id=real.usage_ledger_id,
        work_date=real.work_date, quantity_canonical=real.quantity_canonical, source_digest=real.source_digest,
        request_json=real.request_json, request_digest=real.request_digest, result_json=real.result_json,
        result_digest=real.result_digest)
    sp = db.begin_nested()
    if True:
        db.add(forged)
        db.flush()
        bad = copy.deepcopy(stored)
        bad['source']['consumptions'][0]['id'] = forged.id
        seal(bad)
        assert db.scalar(select(AuditLog.id).where(AuditLog.entity_id == forged.id, AuditLog.action == 'leave_carry_consumed')) is None
        with pytest.raises(ValueError):
            check(db, args, bad)
    sp.rollback()
    check(db, args, stored)


@P
def test_recorded_proof_survives_genuinely_later_history_growth_while_live_net_moves(prior_year, monkeypatch):
    db, args = full_year(prior_year, monkeypatch)
    from app.leave_year2_net_source import review_year2_net_source
    review_year2_net_source(db, **args)
    stored = json.loads(json.dumps(build_year2_recorded_source(db, **args)['stored']))
    db.add(LeaveBalanceTransaction(employee_id=args['employee_id'], leave_type_id=args['leave_type_id'],
        transaction_type='adjustment', days_delta=1, reason='Synthetic later adjustment', created_by='synthetic'))
    db.flush()
    with pytest.raises(ValueError, match='unrelated ledger'):         # the LIVE source refuses later growth ...
        review_year2_net_source(db, **args)
    assert check(db, args, stored)['net']  # ... the recorded proof still verifies, and == Fraction(stored['carried_amount'])   # recorded proof still verifies
    # a NAMED source-bound ledger row cannot be changed at all: the database refuses
    from sqlalchemy.exc import ProgrammingError
    usage = db.get(LeaveBalanceTransaction, stored['source']['consumptions'][0]['usage_ledger_id'])
    usage.days_delta = None
    usage.exact_numerator, usage.exact_denominator = -2, 1
    with pytest.raises(ProgrammingError, match='immutable'):
        db.flush()
    db.rollback()


@P
def test_old_completed_verifier_is_unchanged_and_still_refuses_attribution_shape(prior_year, monkeypatch):
    from app.leave_carry_availability import _verify_recorded_source
    db, args = full_year(prior_year, monkeypatch)
    stored = json.loads(json.dumps(build_year2_recorded_source(db, **args)['stored']))
    own = stored['source']['own']
    net = canonical_amount(Fraction(stored['carried_amount']))
    with pytest.raises(ValueError, match='Carry source identity changed'):
        _verify_recorded_source(db, result=dict(source=own, source_digest=digest(own), source_year=2026,
            carried_amount=net), employee_id=args['employee_id'], leave_type_id=args['leave_type_id'])


@P
def test_named_receipt_audit_is_immutable_so_altering_or_deleting_it_cannot_happen(prior_year, monkeypatch):
    db, args = full_year(prior_year, monkeypatch)
    stored = build_year2_recorded_source(db, **args)['stored']
    first = stored['source']['consumptions'][0]['id']
    audit = db.scalar(select(AuditLog).where(AuditLog.entity_id == first, AuditLog.action == 'leave_carry_consumed'))
    audit.reason = 'Altered'
    with pytest.raises(ValueError, match='immutable'):
        db.flush()
    db.rollback()


@P
def test_expired_old_grant_still_verifies_historical_consumption_nonzero_3(prior_year, monkeypatch):
    """Grant expired by HR inside the year: the recorded proof of its historical consumption still verifies."""
    from tests.test_leave_carry_expiry import prepared_expiry
    from app.leave_carry_expiry import apply_carry_expiry
    from app.models import LeaveMonthEarning, LeaveRegisterEntry, LeaveCarryExpiry
    from app.leave_month_earning import review_month_earning, apply_month_earning
    from datetime import date, timedelta
    from tests.test_l8_year2_net_source import patch_clock, IDENT
    db, call, usage, review = prepared_expiry(prior_year, 'W')
    patch_clock(monkeypatch)
    apply_carry_expiry(db, operation_id='f' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **call)
    emp = usage['employee_id']
    auth = {k: usage[k] for k in IDENT}
    sel_id = db.scalar(select(LeaveMonthEarning.selection_id).where(LeaveMonthEarning.employee_id == emp))
    for month in range(5, 13):
        day = date(2026, month, 1)
        while day.month == month:
            db.add(LeaveRegisterEntry(employee_id=emp, work_date=day, code='', source='synthetic', recorded_by='synthetic'))
            day += timedelta(days=1)
        db.flush()
        months = [date(2026, m, 1) for m in range(1, month + 1)]
        mc = dict(selection_id=sel_id, month_start=date(2026, month, 1),
            evaluated_on=date(2026, month + 1, 1) if month < 12 else date(2027, 1, 1),
            current_insurance_years={(emp, m): None for m in months}, age_evaluation_dates={m: m for m in months}, **auth)
        rv = review_month_earning(db, **mc)
        apply_month_earning(db, operation_id=format(300 + month, '064x'), reviewed_digest=rv['reviewed_digest'],
            human_confirmed=True, reason='Synthetic year 2 month', **mc)
    months = [date(2026, m, 1) for m in range(1, 13)]
    args = dict(employee_id=emp, leave_type_id=usage['leave_type_id'], source_year=2026, selection_id=sel_id,
        reviewed_on=date(2027, 1, 2), current_insurance_years={(emp, m): None for m in months},
        age_evaluation_dates={m: m for m in months}, **auth)
    assert db.scalar(select(LeaveCarryExpiry.id)) is not None
    out = build_year2_recorded_source(db, **args)
    stored = json.loads(json.dumps(out['stored']))
    assert check(db, args, stored)['attributed'] == Fraction(3)
    bad = copy.deepcopy(stored)
    bad['source']['consumptions'][0]['grant_year'] = 2026
    with pytest.raises(ValueError):
        check(db, args, seal(bad))


# ---- additions: annual-kind grant, changed epoch, unnamed history gaps, invalid live grant provenance ----
import inspect
import tests.test_l8_year2_net_source as _net


def _derived(fn, name, cut=None, ret=None):
    """Reuse an existing fixture body verbatim (no edit to the original file): strip decorators, rename, set the return."""
    src = inspect.getsource(fn)
    src = src[src.index('def '):]
    src = src.replace(f'def {fn.__name__}', f'def {name}', 1)
    if cut:
        src = src[:src.index(cut)]
    src += ret
    exec(src, vars(_net))
    return getattr(_net, name)


full_year_sel = _derived(_net.full_year, 'full_year_sel', cut='    return db, args', ret='    return db, args, selection\n')
annual_year = _derived(_net.test_year2_net_annual_category_with_annual_grant_attribution, 'annual_year',
    cut='    result = review_year2_net_source(db, **args)', ret='    return db, args\n')


@P
def test_annual_kind_grant_recorded_proof_verifies_with_real_annual_grant_semantics(prior_year, monkeypatch):
    db, args = annual_year(prior_year, monkeypatch)
    out = build_year2_recorded_source(db, **args)
    stored = json.loads(json.dumps(out['stored']))
    bindings = stored['source']['consumptions']
    assert bindings and all(b['grant_kind'] == 'annual' for b in bindings)
    assert check(db, args, stored)['attributed'] == Fraction(1)
    for name, mutate in {
            'kind_to_carry': lambda s: s['source']['consumptions'][0].__setitem__('grant_kind', 'carry'),
            'grant_year': lambda s: s['source']['consumptions'][0].__setitem__('grant_year', 2026),
            'ceiling_up': lambda s: s['source']['consumptions'][0].__setitem__('grant_ceiling', '999/1'),
            'ceiling_down': lambda s: s['source']['consumptions'][0].__setitem__('grant_ceiling', '1/1')}.items():
        bad = copy.deepcopy(stored)
        mutate(bad)
        with pytest.raises(ValueError):
            check(db, args, seal(bad))
    check(db, args, stored)


@P
def test_swapped_grant_to_a_real_but_other_type_grant_refuses_live_provenance(prior_year, monkeypatch):
    from app.models import LeaveCarryAllocation
    db, args = full_year_sel(prior_year, monkeypatch)[:2]
    stored = json.loads(json.dumps(build_year2_recorded_source(db, **args)['stored']))
    other = db.scalar(select(LeaveCarryAllocation).where(LeaveCarryAllocation.employee_id == args['employee_id'],
        LeaveCarryAllocation.leave_type_id != args['leave_type_id']))
    assert other is not None
    bad = copy.deepcopy(stored)
    bad['source']['consumptions'][0]['grant_id'] = other.id
    with pytest.raises(ValueError):
        check(db, args, seal(bad))


@P
def test_changed_restore_epoch_and_unnamed_later_history_do_not_invalidate_the_recorded_proof(prior_year, monkeypatch):
    from app.models import SystemSetting
    from app.restore_lock import RESTORE_EPOCH_KEY, read_restore_epoch
    from app.leave_year2_net_source import review_year2_net_source
    db, args, selection = full_year_sel(prior_year, monkeypatch)
    stored = json.loads(json.dumps(build_year2_recorded_source(db, **args)['stored']))
    # unnamed later history: another consumption after the proof was built
    from app.leave_carry_consumption import review_carry_consumption, record_carry_consumption
    call = {**selection, 'work_date': date(2026, 1, 17), 'quantity': Fraction(1, 7)}
    review = review_carry_consumption(db, **call)
    record_carry_consumption(db, operation_id='b' * 64, reviewed_digest=review['reviewed_digest'], human_confirmed=True, **call)
    assert check(db, args, stored)['consumptions'] == 2                   # still exactly the two NAMED receipts
    # the enclosing service epoch moves: the old receipt proof stays valid, the LIVE source refuses the old epoch
    before = read_restore_epoch(db)
    row = db.scalar(select(SystemSetting).where(SystemSetting.key == RESTORE_EPOCH_KEY))
    if row is None:
        db.add(SystemSetting(key=RESTORE_EPOCH_KEY, value='epoch-2', updated_by='synthetic'))
    else:
        row.value = 'epoch-2'
    db.flush()
    assert read_restore_epoch(db) == 'epoch-2' and read_restore_epoch(db) != before
    assert check(db, args, stored)['net'] == Fraction(stored['carried_amount'])
    with pytest.raises(ValueError, match='(?i)epoch'):
        review_year2_net_source(db, **args)


@P
def test_changed_register_day_code_of_a_named_usage_day_refuses(prior_year, monkeypatch):
    from app.models import LeaveRegisterEntry
    db, args = full_year(prior_year, monkeypatch)
    stored = json.loads(json.dumps(build_year2_recorded_source(db, **args)['stored']))
    work_date = date.fromisoformat(stored['source']['consumptions'][0]['work_date'])
    entry = db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id == args['employee_id'],
        LeaveRegisterEntry.work_date == work_date))
    old = entry.code
    entry.code = ''
    db.flush()
    with pytest.raises(ValueError, match='register-day binding'):
        check(db, args, stored)
    entry.code = old
    db.flush()
    check(db, args, stored)


@P
def test_tampered_or_resealed_stored_grant_receipt_cannot_be_staged_or_is_refused(prior_year, monkeypatch):
    """Grant receipt rows: either the database refuses the edit, or the historical grant verifier refuses it."""
    from app.models import LeaveCarryAllocation
    from sqlalchemy.exc import DBAPIError
    db, args = full_year(prior_year, monkeypatch)
    stored = json.loads(json.dumps(build_year2_recorded_source(db, **args)['stored']))
    grant = db.get(LeaveCarryAllocation, stored['source']['consumptions'][0]['grant_id'])
    result = json.loads(grant.result_json)
    result['carried_amount'] = '999/1'
    outcome = 'refused-by-verifier'
    grant.result_json = json.dumps(result, sort_keys=True, separators=(',', ':'))
    try:
        db.flush()
    except (DBAPIError, ValueError):
        outcome = 'refused-by-database'
        db.rollback()
    if outcome == 'refused-by-verifier':
        with pytest.raises(ValueError):
            check(db, args, stored)
        grant.result_json = json.dumps(json.loads(grant.result_json))   # not restored; session discarded by fixture
    print('GRANT_TAMPER_OUTCOME', outcome)
    assert outcome in {'refused-by-database', 'refused-by-verifier'}


def test_malformed_amount_and_types_normalize_to_value_error():
    from app.leave_year2_recorded_source import _amount
    for bad in ('1/0', '0/0', '2/4', 'x', 3, None, '1/-2', ' 1/1'):
        with pytest.raises(ValueError):
            _amount(bad, 'x')
    assert _amount('9/7', 'x') == Fraction(9, 7)


@P
@pytest.mark.parametrize('target', ['grant_audit', 'consumption_audit'])
def test_independent_deleted_named_audit_refuses(prior_year, monkeypatch, target):
    from sqlalchemy import text
    db, args = full_year(prior_year, monkeypatch)
    stored = json.loads(json.dumps(build_year2_recorded_source(db, **args)['stored']))
    binding = stored['source']['consumptions'][0]
    entity_id = binding['grant_id'] if target == 'grant_audit' else binding['id']
    db.execute(text('ALTER TABLE audit_logs DISABLE TRIGGER USER'))
    try:
        db.execute(text('DELETE FROM audit_logs WHERE entity_id=:id'), {'id': entity_id})
    finally:
        db.execute(text('ALTER TABLE audit_logs ENABLE TRIGGER USER'))
    db.expire_all()
    with pytest.raises(ValueError): check(db, args, stored)


@P
def test_independent_resealed_grant_without_matching_audit_refuses(prior_year, monkeypatch):
    from sqlalchemy import text
    from app.models import LeaveCarryAllocation
    from app.leave_month_earning import canonical
    db, args = full_year(prior_year, monkeypatch)
    stored = json.loads(json.dumps(build_year2_recorded_source(db, **args)['stored']))
    row = db.get(LeaveCarryAllocation, stored['source']['consumptions'][0]['grant_id'])
    result = json.loads(row.result_json); request = json.loads(row.request_json)
    result['available_through'] = '2026-12-31'
    request['plan'] = result; request['reviewed_digest'] = digest(result)
    db.execute(text('ALTER TABLE leave_carry_allocations DISABLE TRIGGER USER'))
    try:
        db.execute(text('UPDATE leave_carry_allocations SET request_json=:q,request_digest=:qd,result_json=:r,result_digest=:rd WHERE id=:id'),
            dict(q=canonical(request), qd=digest(request), r=canonical(result), rd=digest(result), id=row.id))
    finally:
        db.execute(text('ALTER TABLE leave_carry_allocations ENABLE TRIGGER USER'))
    db.expire_all()
    with pytest.raises(ValueError): check(db, args, stored)


# ---- cap boundaries -------------------------------------------------------------------------------------------
# The real consumption service refuses over-cap receipts, so these cases use SYNTHETIC receipts: rows plus audit built
# with fully consistent digests (the way the service writes them) for amounts the service would have refused.
# Everything before the cap arithmetic (receipt chain, audit, usage, grant provenance) is genuinely exercised.
from app.leave_month_earning import canonical


def _forge(db, base_id, new_id, work_date, qty):
    base = db.get(LeaveCarryConsumption, base_id)
    request, result = json.loads(base.request_json), json.loads(base.result_json)
    plan = copy.deepcopy(result['plan'])
    plan['quantity'] = canonical_amount(qty)
    plan['work_date'] = work_date.isoformat()
    plan['usage']['work_date'] = work_date.isoformat()
    result['plan'] = plan
    request['plan'] = plan
    request['reviewed_digest'] = digest(plan)
    row = LeaveCarryConsumption(id=new_id, employee_id=base.employee_id, leave_type_id=base.leave_type_id,
        grant_kind=base.grant_kind, grant_id=base.grant_id, usage_ledger_id=base.usage_ledger_id, work_date=work_date,
        quantity_canonical=plan['quantity'], source_digest=digest(plan), request_json=canonical(request),
        request_digest=digest(request), result_json=canonical(result), result_digest=digest(result))
    db.add(row)
    db.add(AuditLog(actor=request['actor_username'], action='leave_carry_consumed', entity_type='leave_carry_consumption',
        entity_id=new_id, old_value=row.source_digest, new_value=row.result_json, reason=plan['reason']))
    db.flush()
    return dict(id=new_id, source_digest=row.source_digest, result_digest=row.result_digest, grant_kind=row.grant_kind,
        grant_id=row.grant_id, grant_year=2025, grant_ceiling=plan['grant']['ceiling'], usage_ledger_id=row.usage_ledger_id,
        usage_amount=plan['usage']['amount'], work_date=row.work_date.isoformat(), quantity=row.quantity_canonical)


def _with_extra(stored, extras):
    out = copy.deepcopy(stored)
    add = sum((Fraction(b['quantity']) for b in extras), Fraction())
    src = out['source']
    src['consumptions'] += extras
    src['attributed_amount'] = canonical_amount(Fraction(src['attributed_amount']) + add)
    src['net_amount'] = canonical_amount(Fraction(src['net_amount']) + add)
    out['carried_amount'] = src['net_amount']
    return seal(out)


@P
def test_cap_boundaries_day_exact_passes_over_refuses_and_row_exact_passes(prior_year, monkeypatch):
    """Jan 2026: W days 3/10/17, Jan usage row = 3, ceiling 365/7. Existing receipts: Jan 3 = 1, Jan 10 = 2/7."""
    db, args = full_year(prior_year, monkeypatch)
    stored = json.loads(json.dumps(build_year2_recorded_source(db, **args)['stored']))
    base = stored['source']['consumptions'][1]['id']
    jan10, jan17 = date(2026, 1, 10), date(2026, 1, 17)
    sp = db.begin_nested()
    # day cap EXACT: Jan 10 = 2/7 + 5/7 = 1; row total = 2 <= 3, grant far below ceiling
    exact = _with_extra(stored, [_forge(db, base, '1' * 64, jan10, Fraction(5, 7))])
    assert check(db, args, exact)['attributed'] == Fraction(9, 7) + Fraction(5, 7)
    # row EXACT: add Jan 17 = 1 -> row total 1 + 1 + 1 = 3 == usage amount, every day <= 1
    both = _with_extra(stored, [exact['source']['consumptions'][-1], _forge(db, base, '2' * 64, jan17, Fraction(1))])
    assert check(db, args, both)['attributed'] == Fraction(9, 7) + Fraction(5, 7) + 1
    sp.rollback()
    # day cap OVER: Jan 10 = 2/7 + 6/7 = 8/7 (row total 15/7 <= 3): the day cap, and only it, fires
    sp = db.begin_nested()
    over = _with_extra(stored, [_forge(db, base, '3' * 64, jan10, Fraction(6, 7))])
    with pytest.raises(ValueError, match='day cap'):
        check(db, args, over)
    sp.rollback()
    check(db, args, stored)


@P
def test_usage_row_cap_is_not_reachable_independently_of_the_day_cap(prior_year, monkeypatch):
    """Usage amount == number of register days (1 each) so exceeding the row needs a day over 1: the day cap fires first."""
    db, args = full_year(prior_year, monkeypatch)
    stored = json.loads(json.dumps(build_year2_recorded_source(db, **args)['stored']))
    base = stored['source']['consumptions'][1]['id']
    sp = db.begin_nested()
    extras = [_forge(db, base, '4' * 64, date(2026, 1, 10), Fraction(5, 7)), _forge(db, base, '5' * 64, date(2026, 1, 17), Fraction(1)),
              _forge(db, base, '6' * 64, date(2026, 1, 17), Fraction(1, 7))]            # row 3 + 1/7 > 3, Jan 17 = 8/7
    with pytest.raises(ValueError, match='day cap'):
        check(db, args, _with_extra(stored, extras))
    sp.rollback()


@P
def test_fixture_grant_cap_exceeds_its_nine_register_days(prior_year, monkeypatch):
    """Ceiling 365/7 (about 52) against at most one day per W register day inside the grant window (Jan 1 to Apr 30)."""
    from app.models import LeaveRegisterEntry
    db, args = full_year(prior_year, monkeypatch)
    stored = json.loads(json.dumps(build_year2_recorded_source(db, **args)['stored']))
    b = stored['source']['consumptions'][0]
    days = db.scalars(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id == args['employee_id'],
        LeaveRegisterEntry.code == 'W', LeaveRegisterEntry.work_date >= date(2026, 1, 1),
        LeaveRegisterEntry.work_date <= date(2026, 4, 30))).all()
    assert len(days) == 9 and Fraction(len(days)) < Fraction(b['grant_ceiling'])
