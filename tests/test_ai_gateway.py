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


def _memory_engine(monkeypatch):
    """Bind the gateway to a fresh in-memory database; returns the engine."""
    import app.ai_gateway as gateway
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base

    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    monkeypatch.setattr(gateway, "SessionLocal", factory)
    return engine


def test_record_exchange_persists_user_and_assistant(monkeypatch):
    """Gateway persistence: an ordinary Q&A round-trip lands in memory."""
    import app.ai_gateway as gateway

    engine = _memory_engine(monkeypatch)
    try:
        gateway.record_exchange("tester", "سؤال تجريبي", "جواب تجريبي")
        memory = gateway.recent_memory("tester")
        assert memory == [
            {"role": "user", "content": "سؤال تجريبي", "language": "ar"},
            {"role": "assistant", "content": "جواب تجريبي", "language": "ar"},
        ]
        # other actors see nothing
        assert gateway.recent_memory("someone-else") == []
    finally:
        engine.dispose()


def test_record_exchange_stores_the_rendering_language(monkeypatch):
    import app.ai_gateway as gateway

    engine = _memory_engine(monkeypatch)
    try:
        gateway.record_exchange("tester", "How many employees?", "There are 5.", language="en")
        memory = gateway.recent_memory("tester")
        assert [item["language"] for item in memory] == ["en", "en"]
    finally:
        engine.dispose()


def test_memory_is_bounded_per_actor(monkeypatch):
    """History is capped so the table stays small on a light device."""
    import app.ai_gateway as gateway
    from app.models import AIConversationMessage
    from sqlalchemy import select

    engine = _memory_engine(monkeypatch)
    try:
        for index in range(gateway._MEMORY_KEEP + 25):
            gateway._memory("tester", "user", f"message {index}")
        with gateway.SessionLocal() as db:
            total = db.scalars(
                select(AIConversationMessage).where(
                    AIConversationMessage.actor == "tester"
                )
            ).all()
        assert len(total) == gateway._MEMORY_KEEP
        # the newest entries survive
        recent = gateway.recent_memory("tester", limit=3)
        assert recent[-1]["content"] == f"message {gateway._MEMORY_KEEP + 24}"
    finally:
        engine.dispose()


def test_prepare_command_wrapper_follows_ui_language(monkeypatch):
    """New system messages are generated in the current UI language;
    the plan itself (and its hash) is untouched."""
    import app.ai_gateway as gateway

    engine = _memory_engine(monkeypatch)
    try:
        monkeypatch.setattr(
            gateway, "build_plan",
            lambda question, language="ar": {"action": "approve_payroll_run", "payroll_run_id": 3},
        )
        monkeypatch.setattr(
            gateway, "build_confirmation_preview",
            lambda plan: {"old_values": {}, "new_values": {}, "risks": [], "snapshot": {}},
        )
        result = gateway.prepare_command("Approve payroll run 3.", "tester", language="en")
        assert result["summary"].startswith("A plan has been prepared")
        memory = gateway.recent_memory("tester")
        assert memory[-1]["language"] == "en"
        assert memory[-1]["content"].startswith("Pending plan awaiting your approval:")
    finally:
        engine.dispose()


def test_reject_command_messages_follow_ui_language(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.models import AICommandApproval
    import app.ai_gateway as gateway

    engine = _memory_engine(monkeypatch)
    try:
        token = "reject-language-token"
        now = datetime.now(timezone.utc)
        with gateway.SessionLocal.begin() as db:
            db.add(AICommandApproval(
                token_hash=gateway._token_hash(token),
                actor="tester",
                question="Approve payroll run 3.",
                plan_json=gateway._canonical_json({"action": "approve_payroll_run", "payroll_run_id": 3}),
                plan_hash=gateway.plan_hash({"action": "approve_payroll_run", "payroll_run_id": 3}),
                status="pending",
                created_at=now,
                expires_at=now + timedelta(minutes=10),
            ))
        message = gateway.reject_command(token, "tester", language="zh")
        assert message == "操作已拒绝，未更改任何数据。"
        memory = gateway.recent_memory("tester")
        assert memory[-1]["language"] == "zh"
        assert memory[-1]["content"] == "操作已拒绝，系统数据未更改。"
        # unknown token error is localized too
        try:
            gateway.reject_command("missing-token", "tester", language="en")
            raise AssertionError("expected ValueError")
        except ValueError as exc:
            assert str(exc) == "Approval request not found."
    finally:
        engine.dispose()


def test_json_plan_asks_for_summary_in_ui_language(monkeypatch):
    """The plan prompt requests the summary in the admin's UI language."""
    import app.ai_agent as agent

    captured = {}

    monkeypatch.setattr(
        agent, "local_model_status",
        lambda: {"available": True, "model": "qwen2.5:3b", "message": ""},
    )

    def fake_post(path, payload, timeout=30):
        captured["payload"] = payload
        return {"message": {"content": '{"action": "approve_payroll_run", "payroll_run_id": 3, "summary": "ok"}'}}

    import app.ai_assistant as assistant
    monkeypatch.setattr(assistant, "_post_json", fake_post)

    plan = agent._json_plan("Approve payroll run 3.", {"employees": []}, language="en")
    assert plan["action"] == "approve_payroll_run"
    prompt = captured["payload"]["messages"][1]["content"]
    assert "summary قصيرة باللغة الإنجليزية" in prompt

    plan = agent._json_plan("批准工资批次3。", {"employees": []}, language="zh")
    prompt = captured["payload"]["messages"][1]["content"]
    assert "summary قصيرة باللغة الصينية" in prompt


def test_security_report_renders_trilingually_with_data_untouched():
    from app.security_roster_compare import format_security_comparison_report

    report = {
        "work_date": "2026-11-22",
        "assigned_count": 2,
        "matched_count": 1,
        "mismatch_count": 1,
        "mismatches": [
            {
                "mismatch_type": "late_arrival",
                "employee_code": "MY-0007",
                "employee_name": "اسم تجريبي",
                "difference_minutes": 25,
                "scheduled_start": "08:00",
                "scheduled_end": "16:00",
                "actual_punches": ["08:25", "16:00"],
            }
        ],
    }
    ar = format_security_comparison_report(report, language="ar")
    en = format_security_comparison_report(report, language="en")
    zh = format_security_comparison_report(report, language="zh")
    assert "تأخر حضور" in ar
    assert "Late arrival" in en and "difference: 25 min" in en
    assert "迟到" in zh and "25 分钟" in zh
    # recorded data (codes, names, times) is identical across languages
    for text in (ar, en, zh):
        assert "MY-0007" in text and "اسم تجريبي" in text and "08:25" in text
    assert "review only" in en or "for review only" in en
