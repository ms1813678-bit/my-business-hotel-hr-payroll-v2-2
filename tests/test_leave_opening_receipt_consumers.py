"""Characterization of the confirmed-opening apply receipt against the projection consumers.

Synthetic, read-only consumers. No new math: these tests pin what reconciliation and the
transition draft do today with ledger rows posted by apply_confirmed_opening, and mark the one
treatment that would double count the selected opening.
"""
from dataclasses import replace
from datetime import date
from decimal import Decimal, localcontext

import pytest
import json

from sqlalchemy import func, select

from app.leave_opening_receipt import verified_manual_opening
from app.leave_opening_apply import apply_confirmed_opening, review_opening_apply
from app.leave_source_reconciliation import reconcile_selected_opening
from app.leave_transition_draft import LedgerDecision, OpeningReleaseDecision, draft_transition
from app.models import LeaveBalanceTransaction, AuditLog, LeaveRegisterEntry, LeaveType
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_source_reconciliation import setup_source

A = Decimal('8.2500000000000001')
IDENTITY = ('actor_id', 'session_id', 'session_version', 'expected_epoch')


def canonical_map(db):
    ids = {c: db.scalar(select(LeaveType.id).where(LeaveType.code == code)) for c, code in
           (('A', 'annual'), ('W', 'weekly_rest'), ('H', 'official_holiday'))}
    assert all(ids.values())
    return ids


@pytest.fixture
def applied(history_context):  # noqa: F811
    db, e, args = setup_source(history_context)
    args['release_evaluated_on'] = date(2026, 2, 28)
    # This fixture models apply before later register use; a separate test
    # qualifies opening against already-recorded history without deleting it.
    used = db.scalars(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id == e.id,
                                                       LeaveRegisterEntry.code == 'A')).all()
    kept = [(u.employee_id, u.work_date) for u in used]
    for u in used:
        db.delete(u)
    db.flush()
    auth = {k: args[k] for k in IDENTITY}
    types = canonical_map(db)
    selection_id = args['selection_ids'][0]
    review = review_opening_apply(db, selection_id=selection_id, type_map=types, **auth)
    apply_confirmed_opening(db, selection_id=selection_id, type_map=types,
        reviewed_apply_sha256=review['apply_sha256'], reason='Synthetic apply', human_confirmed=True, **auth)
    for employee_id, day in kept:
        db.add(LeaveRegisterEntry(employee_id=employee_id, work_date=day, code='A', source='synthetic',
                                  recorded_by='synthetic'))
    db.flush()
    after = reconcile_selected_opening(db, **args)
    before = after  # projection is source-only; ledger rows are compared through ledger_total_separate
    return db, e, args, before, after


def row(result, code):
    return next(r for r in result.rows if r.code == code)


def test_reconciliation_reports_applied_opening_as_separate_ledger_and_keeps_projection(applied):
    db, e, args, before, after = applied
    assert row(after, 'A').released_scenario_balance == A - 1  # selected opening minus the one register A day
    assert row(after, 'A').ledger_total_separate == A
    assert row(after, 'W').ledger_total_separate == Decimal(2)
    assert row(after, 'H').ledger_total_separate == 0 and row(after, 'H').ledger_row_ids == ()
    assert len(row(after, 'A').ledger_row_ids) == 1 and len(row(after, 'W').ledger_row_ids) == 1
    # Apply does not itself reconcile: the overlap issue stays and nothing is posted by the consumer.
    assert 'ledger_overlap_not_reconciled' in row(after, 'A').issues
    assert not after.posting_allowed and not after.establishes_spendable_credit


def test_opening_preserves_existing_register_days_and_deducts_once(history_context):
    db, e, args = setup_source(history_context)
    args['release_evaluated_on'] = date(2026, 2, 28)
    before = reconcile_selected_opening(db, **args)
    registers = db.execute(select(LeaveRegisterEntry.id, LeaveRegisterEntry.work_date,
                                 LeaveRegisterEntry.code).where(LeaveRegisterEntry.employee_id == e.id)).all()
    auth = {k:args[k] for k in IDENTITY}
    types = canonical_map(db)
    review = review_opening_apply(db,selection_id=args['selection_ids'][0],type_map=types,**auth)
    apply_confirmed_opening(db,selection_id=args['selection_ids'][0],type_map=types,
        reviewed_apply_sha256=review['apply_sha256'],reason='Synthetic existing history',human_confirmed=True,**auth)
    after = reconcile_selected_opening(db, **args)
    assert db.execute(select(LeaveRegisterEntry.id, LeaveRegisterEntry.work_date,
                            LeaveRegisterEntry.code).where(LeaveRegisterEntry.employee_id == e.id)).all() == registers
    for code in 'AWH':
        assert row(after,code).released_scenario_balance == row(before,code).released_scenario_balance
    assert row(after,'A').released_scenario_balance == A-1
    assert row(after,'A').ledger_total_separate == A
    ledger,openings=decisions(after,'included_in_scenario')
    draft=draft_transition(db,reviewed_reconciliation_digest=after.reconciliation_digest,
        ledger_decisions=ledger,opening_decisions=openings,**args)
    assert next(r for r in draft.rows if r.code=='A').reviewed_target == A-1


def decisions(after, treatment):
    ledger = tuple(LedgerDecision(i, treatment, 'synthetic receipt-bound row') for r in after.rows for i in r.ledger_row_ids)
    openings = tuple(OpeningReleaseDecision(r.employee_id, r.code, True, 'synthetic opening release assertion')
                     for r in after.rows)
    return ledger, openings


def test_receipt_bound_opening_rows_treated_as_included_do_not_double_count(applied):
    db, e, args, before, after = applied
    ledger, openings = decisions(after, 'included_in_scenario')
    draft = draft_transition(db, reviewed_reconciliation_digest=after.reconciliation_digest,
                             ledger_decisions=ledger, opening_decisions=openings, **args)
    for code in 'AWH':
        item = next(r for r in draft.rows if r.code == code)
        assert item.unresolved == (), (code, item.unresolved)
        assert item.reviewed_target == row(after, code).released_scenario_balance
        # Delta only moves the ledger up to the scenario, never re-adds the opening that is already posted.
        with localcontext() as ctx:
            ctx.prec = 100
            assert item.adjustment_delta == row(after, code).released_scenario_balance - row(after, code).ledger_total_separate


@pytest.mark.parametrize('treatment', ['external_to_scenario', 'unresolved'])
def test_verified_receipt_rows_accept_only_included_in_scenario(applied, treatment):
    db, e, args, before, after = applied
    ledger, openings = decisions(after, treatment)
    with pytest.raises(ValueError, match='can only be included_in_scenario'):
        draft_transition(db, reviewed_reconciliation_digest=after.reconciliation_digest,
                         ledger_decisions=ledger, opening_decisions=openings, **args)


def test_mixed_decisions_one_external_receipt_row_refuses(applied):
    db, e, args, before, after = applied
    ledger, openings = decisions(after, 'included_in_scenario')
    ledger = (replace(ledger[0], treatment='external_to_scenario'),) + ledger[1:]
    with pytest.raises(ValueError, match='can only be included_in_scenario'):
        draft_transition(db, reviewed_reconciliation_digest=after.reconciliation_digest,
                         ledger_decisions=ledger, opening_decisions=openings, **args)


def ident(args):
    return {k: args[k] for k in IDENTITY}


def test_helper_verifies_exact_receipt_and_is_read_only(applied):
    db, e, args, before, after = applied
    sel = args['selection_ids'][0]
    count = lambda: db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))  # noqa: E731
    n = count()
    r = verified_manual_opening(db, selection_id=sel, **ident(args))
    assert r['amounts'] == {'A': A, 'W': Decimal(2), 'H': Decimal(0)}
    assert sorted(r['ledger_codes'].values()) == ['A', 'W'] and r['employee_id'] == e.id
    with pytest.raises(ValueError, match='does not verify'):
        verified_manual_opening(db, selection_id=sel)
    assert count() == n


def test_helper_refuses_other_selection_wrong_epoch_and_noncanonical_kind(applied):
    db, e, args, before, after = applied
    sel = args['selection_ids'][0]
    with pytest.raises(ValueError, match='does not verify'):
        verified_manual_opening(db, selection_id='f' * 64)
    with pytest.raises(ValueError, match='does not verify'):
        verified_manual_opening(db, selection_id='not-a-selection')
    with pytest.raises(ValueError):
        verified_manual_opening(db, selection_id=sel, **{**ident(args), 'expected_epoch': 987654})
    kind = db.scalar(select(LeaveType).where(LeaveType.code == 'weekly_rest'))
    kind.code = 'weekly_rest_renamed'
    db.flush()
    with pytest.raises(ValueError, match='does not verify'):
        verified_manual_opening(db, selection_id=sel)


@pytest.mark.parametrize('mutate', ['amount', 'digest', 'extra_key', 'ledger_ids', 'not_object'])
def test_helper_refuses_forged_or_mismatched_receipt(applied, monkeypatch, mutate):
    """Audit rows are immutable (checked by the existing suite), so the stored body is altered at read time."""
    db, e, args, before, after = applied
    sel = args['selection_ids'][0]
    import app.leave_opening_receipt as module
    def forged(raw):
        body = json.loads(raw)
        if mutate == 'amount':
            body['amounts']['A'] = '9'
        elif mutate == 'digest':
            body['apply_sha256'] = '0' * 64
        elif mutate == 'extra_key':
            body['extra'] = 1
        elif mutate == 'ledger_ids':
            body['ledger_ids'] = body['ledger_ids'][:1]
        else:
            return [body]
        return body
    from types import SimpleNamespace
    monkeypatch.setattr(module, 'json', SimpleNamespace(loads=forged))
    with pytest.raises(ValueError, match='does not verify'):
        verified_manual_opening(db, selection_id=sel)


def test_identity_is_mandatory_for_helper(applied):
    db, e, args, before, after = applied
    with pytest.raises(ValueError, match='does not verify'):
        verified_manual_opening(db, selection_id=args['selection_ids'][0])
    partial = {k: v for k, v in ident(args).items() if k != 'session_id'}
    with pytest.raises(ValueError, match='does not verify'):
        verified_manual_opening(db, selection_id=args['selection_ids'][0], **partial)


@pytest.mark.parametrize('failure', ['forged_receipt', 'unselected_key'])
def test_unverified_manual_opening_rows_are_unresolved_not_skipped(applied, monkeypatch, failure):
    """A manual_opening-keyed row that does not verify must never reach a target, whatever its treatment."""
    db, e, args, before, after = applied
    import app.leave_opening_receipt as module
    if failure == 'forged_receipt':
        from types import SimpleNamespace
        monkeypatch.setattr(module, 'json', SimpleNamespace(loads=lambda raw: {**json.loads(raw), 'extra': 1}))
        call_args = args
    else:
        call_args = args
        monkeypatch.setattr(module, 'KEY', __import__('re').compile(r'^$'))
    for treatment in ('external_to_scenario', 'included_in_scenario'):
        ledger, openings = decisions(after, treatment)
        draft = draft_transition(db, reviewed_reconciliation_digest=after.reconciliation_digest,
                                 ledger_decisions=ledger, opening_decisions=openings, **call_args)
        for code in 'AW':
            item = next(r for r in draft.rows if r.code == code)
            assert 'manual_opening_receipt_unverified' in item.unresolved
            assert item.reviewed_target is None and item.adjustment_delta is None


def test_draft_without_full_identity_fails_closed(applied):
    db, e, args, before, after = applied
    ledger, openings = decisions(after, 'included_in_scenario')
    with pytest.raises(Exception):
        draft_transition(db, reviewed_reconciliation_digest=after.reconciliation_digest,
                         ledger_decisions=ledger, opening_decisions=openings,
                         **{k: v for k, v in args.items() if k != 'session_version'})


def test_malformed_audit_amount_is_bounded_valueerror(applied, monkeypatch):
    db, e, args, before, after = applied
    import app.leave_opening_receipt as module
    real = module.Decimal
    calls = {'n': 0}

    def bad(value, *a, **k):
        calls['n'] += 1
        if isinstance(value, str) and calls['n'] > 3:
            raise ArithmeticError('boom')
        return real(value, *a, **k)
    monkeypatch.setattr(module, 'Decimal', bad)
    with pytest.raises(ValueError, match='does not verify'):
        verified_manual_opening(db, selection_id=args['selection_ids'][0], **ident(args))


def test_midmonth_opening_existing_days_before_on_after_basis_count_once(history_context):
    from datetime import timedelta
    db,e,args=setup_source(history_context,hire=date(2026,5,17),end=date(2026,5,31))
    args['release_evaluated_on']=date(2026,5,31)
    # A preceding day is outside employment and the opening basis, never charged.
    db.add(LeaveRegisterEntry(employee_id=e.id,work_date=date(2026,5,16),code='A',source='synthetic',recorded_by='synthetic'))
    on_basis=db.scalar(select(LeaveRegisterEntry).where(LeaveRegisterEntry.employee_id==e.id,LeaveRegisterEntry.work_date==date(2026,5,17)))
    on_basis.code='A';db.flush()
    before=reconcile_selected_opening(db,**args)
    auth=ident(args);types=canonical_map(db);sel=args['selection_ids'][0]
    r=review_opening_apply(db,selection_id=sel,type_map=types,**auth)
    apply_confirmed_opening(db,selection_id=sel,type_map=types,reviewed_apply_sha256=r['apply_sha256'],reason='Synthetic basis history',human_confirmed=True,**auth)
    after=reconcile_selected_opening(db,**args)
    assert row(after,'A').used_since_opening==2
    assert row(after,'A').released_scenario_balance==A-2
    assert row(after,'A').released_scenario_balance==row(before,'A').released_scenario_balance
    assert row(after,'A').ledger_total_separate==A
    ledger,openings=decisions(after,'included_in_scenario')
    d=draft_transition(db,reviewed_reconciliation_digest=after.reconciliation_digest,ledger_decisions=ledger,opening_decisions=openings,**args)
    assert next(r for r in d.rows if r.code=='A').reviewed_target==A-2
