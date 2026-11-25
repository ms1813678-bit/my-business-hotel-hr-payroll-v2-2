"""Synthetic migrated-PostgreSQL tests for the isolated opening apply kernel."""
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.leave_manual_opening_groups import persist_manual_opening_group
from app.leave_opening_apply import (
    RECEIPT_ACTION, apply_confirmed_opening, review_opening_apply)
from app.leave_opening_source import confirm_opening_source, review_opening_source
from app.leave_policy_store import OWNER_DEFAULT_POLICY, persist_policy_version
from app.leave_workflow import add_balance_entry, leave_balance
from app.models import (
    AuditLog, LeaveBalanceTransaction, LeaveRecord, LeaveType, PayrollItem, PayrollRun)
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401

EXACT = '8.250000000000000000000001'


def count(db, model):
    return db.scalar(select(func.count()).select_from(model))


def make_types(db, prefix='syn_open_'):
    if prefix == 'syn_open_':
        return {c: db.scalar(select(LeaveType.id).where(LeaveType.code == k)) for c,k in
                (('A','annual'),('W','weekly_rest'),('H','official_holiday'))}
    ids = {}
    for code in ('A', 'W', 'H'):
        kind = LeaveType(code=prefix + code.lower(), name_ar='x' + code, name_en='Synthetic ' + code,
                         is_paid=True, tracks_balance=True, active=True)
        db.add(kind)
        db.flush()
        ids[code] = kind.id
    return ids


def confirmed(db, fact, auth, *, a=EXACT, w='2', h='0', start=date(2026, 5, 17)):
    policy = persist_policy_version(db, policy=OWNER_DEFAULT_POLICY, reason='Synthetic', **auth)
    group = persist_manual_opening_group(db, employee_id=fact.employee_id,
        balances={'A': Decimal(a), 'W': Decimal(w), 'H': Decimal(h)}, effective_start=start,
        reason='Synthetic manual opening', **auth)[0]
    review = review_opening_source(db, group_id=group.id, policy_id=policy.id, **auth)
    row = confirm_opening_source(db, group_id=group.id, policy_id=policy.id,
        expected_previous_id=review['expected_previous_id'], reviewed_source_sha256=review['source_sha256'],
        reason='Synthetic confirmation', human_confirmed=True, **auth)
    return row


@pytest.fixture
def ready(history_context):  # noqa: F811
    db, _account, auth, fact = history_context
    types = make_types(db)
    selection = confirmed(db, fact, auth)
    review = review_opening_apply(db, selection_id=selection.id, type_map=types, **auth)
    args = dict(selection_id=selection.id, type_map=types, reviewed_apply_sha256=review['apply_sha256'],
                reason='Synthetic apply', human_confirmed=True, **auth)
    return db, auth, fact, types, selection, review, args


def test_atomic_exact_apply_receipt_and_zero_category(ready):
    db, auth, fact, types, selection, review, args = ready
    before = count(db, LeaveBalanceTransaction)
    receipt = apply_confirmed_opening(db, **args)
    assert not receipt['replayed'] and len(receipt['ledger_ids']) == 2  # H is zero: no ledger row
    rows = db.execute(select(LeaveBalanceTransaction).where(
        LeaveBalanceTransaction.employee_id == fact.employee_id,
        LeaveBalanceTransaction.leave_type_id.in_(types.values()))).scalars().all()
    by_type = {r.leave_type_id: r for r in rows}
    assert count(db, LeaveBalanceTransaction) == before + 2
    assert by_type[types['A']].days_delta == Decimal(EXACT) and by_type[types['W']].days_delta == Decimal(2)
    assert leave_balance(db, fact.employee_id, types['A']) == Decimal(EXACT)
    assert leave_balance(db, fact.employee_id, types['H']) == 0
    for row in rows:
        assert row.transaction_type == 'opening' and row.source_digest == review['apply_sha256']
        assert row.earning_year is None and row.released_on is None
        assert row.source_operation_key == f'manual_opening:{selection.id}:' + ('A' if row.leave_type_id == types['A'] else 'W')
    audit = db.scalar(select(AuditLog).where(AuditLog.action == RECEIPT_ACTION))
    assert audit.entity_id == selection.id and review['apply_sha256'] in audit.new_value
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == 'leave_balance_opening')) == 2


def test_exact_retry_is_idempotent_and_changed_digest_refuses(ready):
    db, auth, fact, types, selection, review, args = ready
    first = apply_confirmed_opening(db, **args)
    ledger = count(db, LeaveBalanceTransaction)
    again = apply_confirmed_opening(db, **args)
    assert again['replayed'] and again['ledger_ids'] == first['ledger_ids'] and again['receipt_audit_id'] == first['receipt_audit_id']
    assert count(db, LeaveBalanceTransaction) == ledger
    with pytest.raises(ValueError, match='already applied'):
        apply_confirmed_opening(db, **{**args, 'reviewed_apply_sha256': 'a' * 64})


def test_receipt_rows_are_immutable(ready):
    db, _auth, fact, types, _selection, _review, args = ready
    receipt = apply_confirmed_opening(db, **args)
    for statement in ('UPDATE leave_balance_transactions SET days_delta=9 WHERE id=:i',
                      'DELETE FROM leave_balance_transactions WHERE id=:i'):
        with pytest.raises(DBAPIError, match='immutable'), db.begin_nested():
            db.execute(text(statement), {'i': receipt['ledger_ids'][0]})


@pytest.mark.parametrize('change', ['bad_digest', 'not_confirmed', 'blank_reason', 'bad_map_dup', 'bad_map_keys',
                                    'bad_map_bool', 'string_selection'])
def test_input_refusals_write_nothing(ready, change):
    db, _auth, _fact, types, _selection, _review, args = ready
    bad = dict(args)
    if change == 'bad_digest':
        bad['reviewed_apply_sha256'] = 'b' * 64
    elif change == 'not_confirmed':
        bad['human_confirmed'] = 'yes'
    elif change == 'blank_reason':
        bad['reason'] = '  '
    elif change == 'bad_map_dup':
        bad['type_map'] = {**types, 'W': types['A']}
    elif change == 'bad_map_keys':
        bad['type_map'] = {'A': types['A'], 'W': types['W']}
    elif change == 'bad_map_bool':
        bad['type_map'] = {**types, 'A': True}
    else:
        bad['selection_id'] = ''
    ledger, audits = count(db, LeaveBalanceTransaction), count(db, AuditLog)
    with pytest.raises(ValueError):
        apply_confirmed_opening(db, **bad)
    assert count(db, LeaveBalanceTransaction) == ledger and count(db, AuditLog) == audits


def test_stale_state_after_review_refuses(ready):
    db, _auth, _fact, types, _selection, _review, args = ready
    db.get(LeaveType, types['W']).tracks_balance = False
    db.flush()
    with pytest.raises(ValueError, match='balance-tracking'):
        apply_confirmed_opening(db, **args)
    db.get(LeaveType, types['W']).tracks_balance = True
    db.get(LeaveType, types['A']).code = 'syn_open_renamed'
    db.flush()
    with pytest.raises(ValueError, match='stale'):
        apply_confirmed_opening(db, **args)


def test_seeded_weekly_and_holiday_types_track_balance_and_accept(history_context):  # noqa: F811
    db, _account, auth, fact = history_context
    seeded = {c: db.scalar(select(LeaveType.id).where(LeaveType.code == code)) for c, code in
              (('A', 'annual'), ('W', 'weekly_rest'), ('H', 'official_holiday'))}
    selection = confirmed(db, fact, auth)
    assert review_opening_apply(db, selection_id=selection.id, type_map=seeded, **auth)['ledger_applied'] is False


@pytest.mark.parametrize('evidence', ['opening', 'adjustment', 'leave_record',
                                      'closed_month', 'approved_payroll'])
def test_existing_legacy_evidence_refuses_without_writes(ready, evidence):
    db, auth, fact, types, selection, _review, args = ready
    if evidence in ('opening', 'adjustment'):
        kind = db.get(LeaveType, types['A'])
        add_balance_entry(db, employee_id=fact.employee_id, leave_type=kind, transaction_type=evidence,
                          days_delta=Decimal(1), reason='Synthetic legacy', actor='synthetic')
    elif evidence in ('closed_month', 'closed_later_month'):
        from app.models import LeaveMonthClose
        month = 5 if evidence == 'closed_month' else 9
        db.add(LeaveMonthClose(year=2026, month=month, closed_by='synthetic', **close_extra(db)))
    elif evidence == 'approved_payroll':
        db.add(PayrollRun(period_start=date(2026, 5, 1), period_end=date(2026, 5, 28), pay_date=date(2026, 5, 28),
                          status='approved', created_by='synthetic'))
    elif evidence == 'register':
        from app.models import LeaveRegisterEntry
        db.add(LeaveRegisterEntry(employee_id=fact.employee_id, work_date=date(2026, 6, 2), code='W',
                                  source='synthetic', recorded_by='synthetic'))
    elif evidence == 'leave_record':
        db.add(LeaveRecord(employee_id=fact.employee_id, leave_type_id=types['W'], start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 1), calendar_days=1, charged_days=Decimal(1), reason='Synthetic',
            status='draft', created_by='synthetic'))
    else:
        run = PayrollRun(period_start=date(2026, 6, 1), period_end=date(2026, 6, 28), pay_date=date(2026, 6, 28),
                         status='draft', created_by='synthetic')
        db.add(run); db.flush()
        db.add(PayrollItem(payroll_run_id=run.id, employee_id=fact.employee_id, employee_code_snapshot='S',
            employee_name_snapshot='S', department_name_snapshot='S', position_snapshot='S', salary_basis=Decimal(1),
            base_salary=Decimal(1), daily_hours=Decimal(8), absence_leave_reviewed=True))
    db.flush()
    ledger = count(db, LeaveBalanceTransaction)
    with pytest.raises(ValueError, match='refuse|Existing'):
        apply_confirmed_opening(db, **args)
    assert count(db, LeaveBalanceTransaction) == ledger
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == RECEIPT_ACTION)) == 0


@pytest.mark.parametrize('change', ['version', 'session', 'actor_inactive', 'epoch'])
def test_current_identity_and_epoch_rechecked(ready, change):
    from app.models import AdminAccount, AdminSession
    db, auth, _fact, _types, _selection, _review, args = ready
    bad = dict(args)
    if change == 'version':
        bad['session_version'] = auth['session_version'] + 1
    elif change == 'session':
        bad['session_id'] = 'missing-session'
    elif change == 'actor_inactive':
        db.get(AdminAccount, auth['actor_id']).active = False; db.flush()
    else:
        bad['expected_epoch'] = 'older-epoch'
    ledger = count(db, LeaveBalanceTransaction)
    with pytest.raises(ValueError):
        apply_confirmed_opening(db, **bad)
    assert count(db, LeaveBalanceTransaction) == ledger


def test_replaced_selection_refuses_and_original_receipt_still_replays(ready):
    db, auth, fact, types, selection, review, args = ready
    first = apply_confirmed_opening(db, **args)
    # A newer confirmed source for the same employee replaces the selection; a stale apply must refuse.
    policy = persist_policy_version(db, policy=OWNER_DEFAULT_POLICY, reason='Synthetic 2', **auth)
    group = persist_manual_opening_group(db, employee_id=fact.employee_id,
        balances={'A': Decimal(9), 'W': Decimal(1), 'H': Decimal(1)}, effective_start=date(2026, 5, 17),
        reason='Synthetic second', **auth)[0]
    r = review_opening_source(db, group_id=group.id, policy_id=policy.id, **auth)
    newer = confirm_opening_source(db, group_id=group.id, policy_id=policy.id, expected_previous_id=selection.id,
        reviewed_source_sha256=r['source_sha256'], reason='Synthetic replace', human_confirmed=True, **auth)
    with pytest.raises(ValueError, match='Existing ledger|Existing opening|stale'):
        review_opening_apply(db, selection_id=newer.id, type_map=types, **auth)
    assert apply_confirmed_opening(db, **args)['receipt_audit_id'] == first['receipt_audit_id']
    with pytest.raises(ValueError, match='replaced'):
        review_opening_apply(db, selection_id=selection.id, type_map=types, **auth)


def test_injected_failure_rolls_back_every_write(ready, monkeypatch):
    from sqlalchemy import event
    from app.models import AuditLog as Audit
    db, _auth, _fact, _types, _selection, _review, args = ready
    ledger, audits = count(db, LeaveBalanceTransaction), count(db, AuditLog)
    def boom(_mapper, _conn, target):
        if target.action == RECEIPT_ACTION:
            raise RuntimeError('injected receipt failure')
    event.listen(Audit, 'before_insert', boom)
    try:
        with pytest.raises(RuntimeError, match='injected'):
            apply_confirmed_opening(db, **args)
    finally:
        event.remove(Audit, 'before_insert', boom)
    assert count(db, LeaveBalanceTransaction) == ledger and count(db, AuditLog) == audits
    assert apply_confirmed_opening(db, **args)['replayed'] is False


def test_legacy_opening_after_apply_still_refused_and_adjustment_allowed(ready):
    db, _auth, fact, types, _selection, _review, args = ready
    apply_confirmed_opening(db, **args)
    kind = db.get(LeaveType, types['A'])
    with pytest.raises(ValueError, match='already recorded'):
        add_balance_entry(db, employee_id=fact.employee_id, leave_type=kind, transaction_type='opening',
                          days_delta=Decimal(1), reason='Synthetic', actor='synthetic')
    add_balance_entry(db, employee_id=fact.employee_id, leave_type=kind, transaction_type='adjustment',
                      days_delta=Decimal('-1'), reason='Synthetic correction', actor='synthetic')
    assert leave_balance(db, fact.employee_id, types['A']) == Decimal(EXACT) - 1


@pytest.mark.parametrize('change', ['version', 'session', 'actor_inactive', 'epoch', 'session_revoked',
                                    'role', 'must_change_password', 'pending_reset'])
def test_receipt_replay_revalidates_principal_before_returning_receipt(ready, change):
    from app.models import AdminAccount
    db, auth, _fact, _types, _selection, _review, args = ready
    apply_confirmed_opening(db, **args)
    bad = dict(args)
    if change == 'version':
        bad['session_version'] = auth['session_version'] + 1
    elif change == 'session':
        bad['session_id'] = 'missing-session'
    elif change == 'actor_inactive':
        db.get(AdminAccount, auth['actor_id']).active = False
        db.flush()
    elif change in ('role', 'must_change_password', 'pending_reset'):
        account = db.get(AdminAccount, auth['actor_id'])
        if change == 'role':
            account.role = 'finance'
        elif change == 'must_change_password':
            account.must_change_password = True
        else:
            account.pending_credential_reset = True
        db.flush()
    elif change == 'session_revoked':
        from app.models import AdminSession
        db.delete(db.scalar(select(AdminSession).where(AdminSession.id == auth['session_id'])))  # logout/revocation
        db.flush()
    else:
        bad['expected_epoch'] = 'older-epoch'
    with pytest.raises(ValueError):
        apply_confirmed_opening(db, **bad)


def test_replay_with_different_type_map_refuses(ready):
    db, _auth, _fact, types, _selection, _review, args = ready
    apply_confirmed_opening(db, **args)
    other = make_types(db, 'syn_other_')
    with pytest.raises(ValueError, match='type map'):
        apply_confirmed_opening(db, **{**args, 'type_map': other})


@pytest.mark.parametrize('table', ['leave_month_close', 'leave_balance_transactions', 'leave_records', 'leave_register_entries', 'leave_types',
                                   'payroll_items', 'payroll_runs'])
def test_apply_holds_evidence_table_locks_against_concurrent_writers(ready, table):
    """A second real connection cannot take a writer lock on any evidence table while apply is open."""
    db, _auth, _fact, _types, _selection, _review, args = ready
    apply_confirmed_opening(db, **args)
    other = db.get_bind().engine.connect()
    try:
        with pytest.raises(DBAPIError, match='could not obtain lock'):
            other.exec_driver_sql(f'LOCK TABLE {table} IN ROW EXCLUSIVE MODE NOWAIT')
    finally:
        other.close()


def close_extra(db):
    from app.models import BackupRun
    backup = BackupRun(backup_type='manual', status='success', size_bytes=1, initiated_by='synthetic')
    db.add(backup)
    db.flush()
    return {'backup_run_id': backup.id, 'policy_snapshot_json': '{}', 'closed_at': datetime.now(timezone.utc)}


def test_zero_category_blocks_later_legacy_opening(ready):
    db, _auth, fact, types, _selection, _review, args = ready
    apply_confirmed_opening(db, **args)  # H is zero
    with pytest.raises(ValueError):
        add_balance_entry(db, employee_id=fact.employee_id, leave_type=db.get(LeaveType, types['H']),
                          transaction_type='opening', days_delta=Decimal(5), reason='Synthetic', actor='synthetic')


def test_all_zero_opening_receipt_blocks_replacement_and_legacy(history_context):
    db,_account,auth,fact=history_context
    kinds=make_types(db)
    first=confirmed(db,fact,auth,a='0',w='0',h='0')
    r=review_opening_apply(db,selection_id=first.id,type_map=kinds,**auth)
    apply_confirmed_opening(db,selection_id=first.id,type_map=kinds,reviewed_apply_sha256=r['apply_sha256'],reason='Synthetic zero',human_confirmed=True,**auth)
    assert count(db,LeaveBalanceTransaction)==0
    for k in kinds.values():
        with pytest.raises(ValueError,match='already recorded'):
            add_balance_entry(db,employee_id=fact.employee_id,leave_type=db.get(LeaveType,k),transaction_type='opening',days_delta=Decimal(1),reason='Synthetic',actor='synthetic')
    second=confirmed(db,fact,auth,a='1',w='2',h='3')
    with pytest.raises(ValueError,match='Existing opening receipt'):
        review_opening_apply(db,selection_id=second.id,type_map=kinds,**auth)


@pytest.mark.parametrize('raw',['invalid-json','[]','{}','null'])
def test_malformed_legacy_receipt_refuses_without_writes(ready,raw):
    db,_auth,fact,kinds,selection,_review,_args=ready
    db.add(AuditLog(actor='synthetic',action=RECEIPT_ACTION,entity_type='leave_opening_selection',entity_id=selection.id,new_value=raw,reason='Synthetic malformed'))
    db.flush()
    with pytest.raises(ValueError,match='Malformed'):
        add_balance_entry(db,employee_id=fact.employee_id,leave_type=db.get(LeaveType,kinds['H']),transaction_type='opening',days_delta=Decimal(1),reason='Synthetic',actor='synthetic')
    assert count(db,LeaveBalanceTransaction)==0


@pytest.mark.parametrize('raw',['invalid-json','[]','{}','null'])
def test_malformed_kernel_receipt_refuses_review_and_apply(ready,raw):
    db,_auth,_fact,_kinds,selection,_review,args=ready
    db.add(AuditLog(actor='synthetic',action=RECEIPT_ACTION,entity_type='leave_opening_selection',entity_id=selection.id,new_value=raw,reason='Synthetic malformed'))
    db.flush()
    with pytest.raises(ValueError,match='Malformed'):
        review_opening_apply(db,selection_id=selection.id,type_map=args['type_map'],**_auth)
    with pytest.raises(ValueError,match='Malformed'):
        apply_confirmed_opening(db,**args)
    assert count(db,LeaveBalanceTransaction)==0
