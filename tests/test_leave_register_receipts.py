from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.leave_register_receipts import confirm_register_receipt, issue_register_receipt
from app.models import AuditLog, LeaveRegisterEntry, LeaveRegisterReviewReceipt, utcnow
from tests.test_leave_exact_adjustment import adjustment_store  # noqa: F401
from tests.test_leave_precision_migration import precision_schema  # noqa: F401
from tests.test_leave_register_preparation import preparation_store  # noqa: F401


def issued(store):
    engine, edit, kwargs = store
    identity = {key: kwargs[key] for key in ('actor_id', 'session_id', 'session_version')}
    token, _review = issue_register_receipt(engine, edit=edit, **identity)
    return engine, token, identity


def test_receipt_human_confirmation_replay_and_actor_session_binding(preparation_store):  # noqa: F811
    engine, token, identity = issued(preparation_store)
    for changes in ({'actor_id': 2147483647}, {'session_id': 'other-session'}, {'session_version': 1}):
        with pytest.raises(ValueError):
            confirm_register_receipt(engine, token=token, **(identity | changes), human_confirmed=True)
    for confirmation in (False, None, 'true', 1):
        with pytest.raises(ValueError):
            confirm_register_receipt(engine, token=token, **identity, human_confirmed=confirmation)
    result = confirm_register_receipt(engine, token=token, **identity, human_confirmed=True)
    assert result[1] == 1
    with pytest.raises(ValueError):
        confirm_register_receipt(engine, token=token, **identity, human_confirmed=True)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry)) == 1
        assert db.scalar(select(LeaveRegisterReviewReceipt)).consumed_at is not None


def test_receipt_audit_fault_rolls_back_consumption_and_cell(preparation_store):  # noqa: F811
    engine, token, identity = issued(preparation_store)

    def fail(mapper, connection, target):
        raise RuntimeError('Synthetic receipt audit fault')

    event.listen(AuditLog, 'before_insert', fail)
    try:
        with pytest.raises(RuntimeError):
            confirm_register_receipt(engine, token=token, **identity, human_confirmed=True)
    finally:
        event.remove(AuditLog, 'before_insert', fail)
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(LeaveRegisterEntry)) == 0
        assert db.scalar(select(LeaveRegisterReviewReceipt)).consumed_at is None
    assert confirm_register_receipt(engine, token=token, **identity, human_confirmed=True)


def test_receipt_expiry_and_concurrent_consumption(preparation_store):  # noqa: F811
    engine, token, identity = issued(preparation_store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirm_register_receipt, engine, token=token,
            **identity, human_confirmed=True) for _ in range(2)]
        results = []
        for future in futures:
            try:
                results.append(future.result(timeout=10))
            except ValueError:
                results.append('refused')
    assert results.count('refused') == 1
    engine, token, identity = issued(preparation_store)
    with Session(engine) as db, db.begin():
        for receipt in db.scalars(select(LeaveRegisterReviewReceipt)):
            receipt.expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(ValueError):
        confirm_register_receipt(engine, token=token, **identity, human_confirmed=True)
