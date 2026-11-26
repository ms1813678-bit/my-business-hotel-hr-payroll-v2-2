"""Independent adversarial review of leave_zero_decision (0078). Real PG, rolled back."""
import json
from datetime import date

import pytest
from sqlalchemy import func, select, text

from app.leave_cash_out import review_cash_out, verified_released_balance
from app.leave_zero_decision import record_zero_decision, review_zero_decision
from app.models import LeaveBalanceTransaction, LeaveZeroDecision
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_cash_out import payroll
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_zero_decision import IDENT, zero
from tests.test_restore_epoch_rebind import confirm, rebind, restore


def ident(args):
    return {k: args[k] for k in IDENT}


def test_zero_survives_restore_only_through_rebind_lineage(prior_year):
    db, e, kinds, args = prior_year
    _r, c = zero(db, args)
    record_zero_decision(db, **c)
    assert verified_released_balance(db, employee_id=e.id, leave_type_id=args['leave_type_id'], identity=ident(args))['balance'] == 0
    auth2 = restore(db, ident(args), 'E1-restored')
    # zero proof is epoch-free (receipt+ledger+audit); this opening has no ledger rows, so nothing else is epoch-bound
    assert verified_released_balance(db, employee_id=e.id, leave_type_id=args['leave_type_id'], identity=auth2)['balance'] == 0
    new = confirm(db, e, auth2, {k: __import__('decimal').Decimal(0) for k in 'AWH'}, start=e.hire_date)
    rebind(db, args['selection_id'], new.id, auth2)
    bal = verified_released_balance(db, employee_id=e.id, leave_type_id=args['leave_type_id'], identity=auth2)
    assert bal['balance'] == 0
    assert db.scalar(select(func.count()).select_from(LeaveZeroDecision)) == 1  # receipt untouched, no 2nd debit


def test_cash_out_of_zeroed_type_refuses(prior_year):
    db, e, kinds, args = prior_year
    _r, c = zero(db, args)
    record_zero_decision(db, **c)
    _run, item = payroll(db, e, start=date(2026, 1, 1), end=date(2026, 1, 31))
    with pytest.raises(ValueError):
        review_cash_out(db, employee_id=e.id, leave_type_id=args['leave_type_id'], days='1', payroll_item_id=item.id,
                        evaluated_on=date(2026, 2, 2), **ident(args))


def test_replay_and_input_edges_are_value_errors_not_raw(prior_year):
    db, e, kinds, args = prior_year
    _r, c = zero(db, args)
    record_zero_decision(db, **c)
    raw = []
    for name, bad in (('reason_int', {'reason': 123}), ('reason_none', {'reason': None}),
                      ('no_reviewed_on', {'source_selection': {k: v for k, v in args.items() if k != 'reviewed_on'}}),
                      ('no_actor', {'source_selection': {k: v for k, v in args.items() if k != 'actor_id'}})):
        try:
            record_zero_decision(db, **{**c, **bad})
            raw.append((name, 'no-raise'))
        except ValueError:
            pass
        except Exception as exc:  # noqa: BLE001
            raw.append((name, type(exc).__name__))
    assert raw == [], raw


@pytest.mark.parametrize('forge', ['request_list', 'amount_div0', 'ledger_key_other', 'audit_actor'])
def test_forged_stored_proof_refuses_with_value_error(prior_year, forge):
    db, e, kinds, args = prior_year
    _r, c = zero(db, args)
    record_zero_decision(db, **c)
    rid = c['operation_id']
    for t in ('leave_zero_decisions', 'leave_balance_transactions', 'audit_logs'):
        db.execute(text(f'ALTER TABLE {t} DISABLE TRIGGER USER'))
    if forge == 'request_list':
        db.execute(text("UPDATE leave_zero_decisions SET request_json='[]' WHERE id=:i"), {'i': rid})
    elif forge == 'amount_div0':
        row = db.get(LeaveZeroDecision, rid); res = json.loads(row.result_json); res['zeroed_amount'] = '1/0'
        db.execute(text("UPDATE leave_zero_decisions SET result_json=:j WHERE id=:i"), {'j': json.dumps(res), 'i': rid})
    elif forge == 'ledger_released_null':
        db.execute(text("UPDATE leave_balance_transactions SET released_on=NULL WHERE source_operation_key=:k"), {'k': 'leave_zero:' + rid})
    elif forge == 'ledger_key_other':
        db.execute(text("UPDATE leave_balance_transactions SET source_operation_key='leave_zero:'||repeat('c',64) WHERE source_operation_key=:k"), {'k': 'leave_zero:' + rid})
    elif forge == 'audit_actor':
        db.execute(text("UPDATE audit_logs SET actor='someone-else' WHERE action='leave_zero_decided'"))
    db.expire_all()
    with pytest.raises(ValueError):
        verified_released_balance(db, employee_id=e.id, leave_type_id=args['leave_type_id'], identity=ident(args))
