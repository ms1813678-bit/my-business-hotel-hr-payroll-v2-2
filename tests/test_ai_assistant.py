from datetime import date
from decimal import Decimal

from app.ai_assistant import build_system_prompt, local_fallback_answer


def test_ai_system_prompt_is_advisory():
    prompt = build_system_prompt()
    assert "مساعد موارد بشرية محلي" in prompt
    assert "مراجعة وتأكيد" in prompt


def test_ai_fallback_reads_dashboard_metrics():
    answer = local_fallback_answer(
        "كم عدد الموظفين النشطين؟",
        {"metrics": {"active_employees": 7}},
    )
    assert answer == "عدد الموظفين النشطين حاليًا: 7."


def test_ai_fallback_keeps_core_system_usable_when_model_is_unavailable():
    answer = local_fallback_answer(
        "ساعدني في شيء آخر",
        {"metrics": {}},
    )
    assert "غير متاح حاليًا" in answer


def test_ai_command_requires_explicit_execute_prefix():
    from app.ai_agent import is_command_request, wants_execution
    assert is_command_request("اعتمد مسير الرواتب رقم 1")
    assert not wants_execution("اعتمد مسير الرواتب رقم 1")
    assert wants_execution("نفذ: اعتمد مسير الرواتب رقم 1")


def test_ai_command_intent_routing_is_trilingual():
    """EN/ZH write intents route into the same approval gateway; detection
    never executes anything by itself."""
    from app.ai_agent import is_command_request, wants_execution

    # English quick examples
    assert is_command_request("Execute: approve payroll run 3.")
    assert wants_execution("Execute: approve payroll run 3.")
    assert is_command_request("Approve leave request 12 — preview only.")
    assert not wants_execution("Approve leave request 12 — preview only.")
    assert is_command_request("Add an installment advance of EGP 3,000 for employee MY-0004 over 6 months.")
    # Chinese quick examples
    assert is_command_request("执行：批准工资批次3。")
    assert wants_execution("执行：批准工资批次3。")
    assert is_command_request("批准第 12 号休假申请 — 仅预览。")
    assert not wants_execution("批准第 12 号休假申请 — 仅预览。")
    assert is_command_request("添加员工 MY-0004 的 3,000 埃镑分期预支，分 6 个月偿还。")
    # ordinary questions in any language stay on the Q&A path
    assert not is_command_request("How many active employees are there?")
    assert not is_command_request("目前有多少名在职员工？")
    assert not is_command_request("What needs review in attendance this month?")
    assert not is_command_request("本月考勤有哪些需要复核？")
    # case-insensitive English, word-boundary matched
    assert is_command_request("approve payroll run 3")
    assert not is_command_request("payroll runs overview")


def test_ai_command_allowlist_does_not_include_arbitrary_operations():
    from app.ai_agent import ALLOWED_ACTIONS
    assert "approve_payroll_run" in ALLOWED_ACTIONS
    assert "run_sql" not in ALLOWED_ACTIONS
    assert "shell" not in ALLOWED_ACTIONS


def test_employee_management_actions_are_allowlisted():
    from app.ai_agent import ALLOWED_ACTIONS
    for action in ("create_employee", "edit_employee", "transfer_employee", "archive_employee", "rehire_employee"):
        assert action in ALLOWED_ACTIONS


def test_ai_network_policy_accepts_only_loopback():
    from app.ai_agent import _loopback_only
    assert _loopback_only("http://127.0.0.1:11434")
    assert _loopback_only("http://localhost:11434")
    assert not _loopback_only("https://example.com:11434")


def test_ai_command_supports_manual_attendance_and_safe_draft_leave_deletion():
    from app.ai_agent import ALLOWED_ACTIONS, is_command_request

    assert "create_manual_attendance" in ALLOWED_ACTIONS
    assert "delete_draft_leave" in ALLOWED_ACTIONS
    assert is_command_request("سجل حضور يدوي للموظف MY-0001")
    assert is_command_request("احذف مسودة الإجازة رقم 12")


def test_ai_can_delete_only_unconfirmed_draft_leave_with_audit(monkeypatch):
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    from app.models import AuditLog, Department, Employee, LeaveRecord, LeaveType
    import app.ai_agent as agent

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        with factory() as db:
            dept = Department(name="Reception", active=True)
            db.add(dept)
            db.flush()
            emp = Employee(
                code="MY-9011", full_name="موظف حذف تجريبي", department_id=dept.id,
                position="Reception", hire_date=date(2026, 1, 1), status="active",
                basic_salary=Decimal("5000"), daily_hours=Decimal("8"),
            )
            leave_type = LeaveType(
                code="annual", name_ar="سنوية", name_en="Annual", tracks_balance=True, is_paid=True, active=True,
            )
            db.add_all([emp, leave_type])
            db.flush()
            leave = LeaveRecord(
                employee_id=emp.id, leave_type_id=leave_type.id,
                start_date=date(2026, 11, 20), end_date=date(2026, 11, 20),
                calendar_days=1, charged_days=Decimal("1"), reason="مسودة تجريبية",
                status="draft", created_by="admin",
            )
            db.add(leave)
            db.commit()
            leave_id = leave.id

        monkeypatch.setattr(agent, "SessionLocal", factory)
        result = agent.execute_plan(
            {"action": "delete_draft_leave", "record_id": leave_id, "reason": "اختبار حذف مسودة"},
            "admin",
        )
        assert "تم حذف مسودة الإجازة" in result.audit
        with factory() as db:
            assert db.get(LeaveRecord, leave_id) is None
            audit = db.scalar(select(AuditLog).where(AuditLog.action == "ai_draft_leave_deleted"))
            assert audit is not None
            assert audit.entity_id == str(leave_id)
    finally:
        engine.dispose()


def test_ai_refuses_to_delete_confirmed_leave(monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    from app.models import Department, Employee, LeaveRecord, LeaveType
    import app.ai_agent as agent
    import pytest

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    try:
        with factory() as db:
            dept = Department(name="Security", active=True)
            db.add(dept)
            db.flush()
            emp = Employee(
                code="MY-9012", full_name="موظف إجازة مؤكدة", department_id=dept.id,
                position="Security", hire_date=date(2026, 1, 1), status="active",
                basic_salary=Decimal("5000"), daily_hours=Decimal("8"),
            )
            leave_type = LeaveType(code="annual", name_ar="سنوية", name_en="Annual", tracks_balance=True, is_paid=True, active=True)
            db.add_all([emp, leave_type])
            db.flush()
            leave = LeaveRecord(
                employee_id=emp.id, leave_type_id=leave_type.id,
                start_date=date(2026, 11, 20), end_date=date(2026, 11, 20),
                calendar_days=1, charged_days=Decimal("1"), reason="إجازة مؤكدة",
                status="confirmed", created_by="admin",
            )
            db.add(leave)
            db.commit()
            leave_id = leave.id

        monkeypatch.setattr(agent, "SessionLocal", factory)
        with pytest.raises(ValueError, match="مسودة الإجازة فقط"):
            agent.execute_plan(
                {"action": "delete_draft_leave", "record_id": leave_id, "reason": "اختبار منع الحذف"},
                "admin",
            )
        with factory() as db:
            assert db.get(LeaveRecord, leave_id) is not None
    finally:
        engine.dispose()


def test_run_ai_command_never_executes_even_with_explicit_execute_prefix(monkeypatch):
    import app.ai_agent as agent
    import app.ai_gateway as gateway
    from datetime import datetime, timezone

    called = {}
    def fake_prepare(question, actor):
        called["actor"] = actor
        return {
            "token": "one-time-token",
            "plan": {"action": "approve_payroll_run", "payroll_run_id": 9},
            "summary": "اعتماد مسير الرواتب رقم 9",
            "expires_at": datetime(2026, 11, 20, tzinfo=timezone.utc),
        }

    def forbidden_execute(*args, **kwargs):
        raise AssertionError("run_ai_command must never execute a write")

    monkeypatch.setattr(agent, "_loopback_only", lambda url: True)
    monkeypatch.setattr(gateway, "prepare_command", fake_prepare)
    monkeypatch.setattr(agent, "execute_plan", forbidden_execute)
    result = agent.run_ai_command("نفذ: اعتمد مسير الرواتب رقم 9", "hr-admin")

    assert result["executed"] is False
    assert result["pending"] is True
    assert result["token"] == "one-time-token"
    assert called["actor"] == "hr-admin"
