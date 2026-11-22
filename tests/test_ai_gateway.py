import json
from datetime import datetime, timedelta, timezone

from app.ai_gateway import _canonical_json, plan_hash


def test_ai_plan_hash_is_stable_for_key_order():
    first = {"action": "approve_payroll_run", "payroll_run_id": 3}
    second = {"payroll_run_id": 3, "action": "approve_payroll_run"}
    assert _canonical_json(first) == _canonical_json(second)
    assert plan_hash(first) == plan_hash(second)


def test_ai_plan_hash_changes_when_plan_changes():
    first = {"action": "approve_payroll_run", "payroll_run_id": 3}
    changed = {"action": "approve_payroll_run", "payroll_run_id": 4}
    assert plan_hash(first) != plan_hash(changed)


def test_ai_gateway_models_are_local_data_only():
    from app.models import AICommandApproval, AIConversationMessage
    assert AICommandApproval.__tablename__ == "ai_command_approvals"
    assert AIConversationMessage.__tablename__ == "ai_conversation_messages"


def test_ai_approval_expiry_is_time_bounded():
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=10)
    assert expires > now
    assert expires <= now + timedelta(minutes=10)

def test_ai_gateway_normalizes_naive_database_expiry_to_utc():
    from app.ai_gateway import _as_utc

    naive = datetime(2026, 11, 20, 12, 0, 0)
    aware = _as_utc(naive)

    assert aware.tzinfo == timezone.utc
    assert aware == datetime(2026, 11, 20, 12, 0, 0, tzinfo=timezone.utc)


def test_stale_confirmation_snapshot_stays_invalidated_and_never_executes(monkeypatch):
    import pytest
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    from app.models import AICommandApproval
    import app.ai_gateway as gateway

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    token = "stale-preview-token"
    now = datetime.now(timezone.utc)
    plan = {
        "action": "edit_employee",
        "employee_id": 5,
        "_confirmation_snapshot": {
            "action": "edit_employee",
            "target": {"type": "employee", "id": 5},
            "current": {"basic_salary": "5000"},
        },
    }
    try:
        with factory.begin() as db:
            db.add(AICommandApproval(
                token_hash=gateway._token_hash(token),
                actor="hr-admin",
                question="تغيير راتب الموظف",
                plan_json=gateway._canonical_json(plan),
                plan_hash=gateway.plan_hash(plan),
                status="pending",
                created_at=now,
                expires_at=now + timedelta(minutes=10),
            ))

        monkeypatch.setattr(gateway, "SessionLocal", factory)
        monkeypatch.setattr(
            gateway,
            "build_confirmation_preview",
            lambda candidate: {
                "snapshot": {
                    "action": "edit_employee",
                    "target": {"type": "employee", "id": 5},
                    "current": {"basic_salary": "6500"},
                }
            },
        )
        monkeypatch.setattr(
            gateway,
            "execute_plan",
            lambda *args, **kwargs: pytest.fail("stale approval must never execute"),
        )

        with pytest.raises(ValueError, match="تغيرت بيانات السجل"):
            gateway.approve_command(token, "hr-admin")

        with factory() as db:
            approval = db.scalar(select(AICommandApproval).where(
                AICommandApproval.token_hash == gateway._token_hash(token)
            ))
            assert approval.status == "invalidated"
            assert approval.approved_at is None
    finally:
        engine.dispose()


def test_leave_balance_preview_shows_current_and_resulting_balance_and_rejects_stale_execution(monkeypatch):
    from datetime import date
    from decimal import Decimal

    import pytest
    from sqlalchemy import create_engine, select, func
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.db import Base
    from app.models import Department, Employee, LeaveType, LeaveBalanceTransaction
    import app.ai_gateway as gateway
    import app.ai_agent as agent
    from app.leave_workflow import leave_balance

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        with factory.begin() as db:
            department = Department(name="Reception", active=True)
            leave_type = LeaveType(
                code="annual", name_ar="سنوية", name_en="Annual",
                is_paid=True, tracks_balance=True, active=True,
            )
            db.add_all([department, leave_type])
            db.flush()
            employee = Employee(
                code="MY-9020", full_name="موظف رصيد تجريبي",
                department_id=department.id, position="Reception",
                hire_date=date(2026, 1, 1), status="active",
                basic_salary=Decimal("5000"), daily_hours=Decimal("8"),
            )
            db.add(employee)
            db.flush()
            db.add(LeaveBalanceTransaction(
                employee_id=employee.id, leave_type_id=leave_type.id,
                transaction_type="opening", days_delta=Decimal("5"),
                reason="رصيد افتتاحي تجريبي", created_by="test",
            ))
            employee_id, leave_type_id = employee.id, leave_type.id

        monkeypatch.setattr(gateway, "SessionLocal", factory)
        monkeypatch.setattr(agent, "SessionLocal", factory)
        plan = {
            "action": "adjust_leave_balance",
            "employee_id": employee_id,
            "leave_type_id": leave_type_id,
            "balance_days": "2",
            "transaction_type": "adjustment",
            "reason": "تصحيح رصيد تجريبي",
        }
        preview = gateway.build_confirmation_preview(plan)
        assert preview["old_values"]["current_balance"] == "5.00"
        assert preview["new_values"]["resulting_balance"] == "7.00"

        # Simulate another approved HR change after the preview was shown.
        with factory.begin() as db:
            leave_type = db.get(LeaveType, leave_type_id)
            from app.leave_workflow import add_balance_entry
            add_balance_entry(
                db, employee_id=employee_id, leave_type=leave_type,
                transaction_type="adjustment", days_delta=Decimal("1"),
                reason="تغيير متزامن", actor="other-admin",
            )

        plan["_confirmation_snapshot"] = preview["snapshot"]
        with pytest.raises(ValueError, match="تغير رصيد الإجازة بعد عرض المعاينة"):
            agent.execute_plan(plan, "hr-admin")

        with factory() as db:
            assert leave_balance(db, employee_id, leave_type_id) == Decimal("6.00")
            count = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
            assert count == 2
    finally:
        engine.dispose()


def test_record_exchange_persists_user_and_assistant(monkeypatch):
    """Gateway persistence: an ordinary Q&A round-trip lands in memory."""
    import app.ai_gateway as gateway
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    try:
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        monkeypatch.setattr(gateway, "SessionLocal", factory)
        gateway.record_exchange("tester", "سؤال تجريبي", "جواب تجريبي")
        memory = gateway.recent_memory("tester")
        assert memory == [
            {"role": "user", "content": "سؤال تجريبي"},
            {"role": "assistant", "content": "جواب تجريبي"},
        ]
        # other actors see nothing
        assert gateway.recent_memory("someone-else") == []
    finally:
        engine.dispose()
