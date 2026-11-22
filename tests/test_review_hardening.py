"""Independent-review hardening: docs gate, security headers, localized audit
detail payloads, and per-locale leave type names."""
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.audit_display import detail_label
from app.i18n import translate_text
from app.main import _leave_type_name, app


def test_docs_and_openapi_hidden_by_default():
    client = TestClient(app)
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404


def test_security_headers_present():
    response = TestClient(app).get("/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "same-origin"


def test_audit_detail_labels_localize_known_keys():
    payload = "code=EMP-001; full_name=Ahmed Ali; department_id=2; branch_id=1"
    assert detail_label(payload, "ar") == "الكود=EMP-001; الاسم الكامل=Ahmed Ali; القسم=2; الفرع=1"
    assert detail_label(payload, "en") == "Code=EMP-001; Full name=Ahmed Ali; Department=2; Branch=1"
    assert detail_label(payload, "zh") == "代码=EMP-001; 姓名=Ahmed Ali; 部门=2; 分支机构=1"


def test_audit_detail_preserves_unknown_payloads():
    assert detail_label("free text without keys", "en") == "free text without keys"
    assert detail_label("unknown_key=1; note=code=2", "en") == "unknown_key=1; Note=code=2"
    assert detail_label("", "en") == ""


def test_leave_type_name_per_locale():
    item = SimpleNamespace(name_ar="إجازة سنوية", name_en="Annual leave")
    assert _leave_type_name(item, "ar") == "إجازة سنوية"
    assert _leave_type_name(item, "en") == "Annual leave"
    assert _leave_type_name(item, "zh") == "年假"


def test_leave_type_name_custom_type_zh_falls_back_to_english():
    item = SimpleNamespace(name_ar="إجازة خاصة جدًا", name_en="Very special leave")
    assert _leave_type_name(item, "zh") == "Very special leave"
    item_no_en = SimpleNamespace(name_ar="إجازة خاصة جدًا", name_en="")
    assert _leave_type_name(item_no_en, "zh") == "إجازة خاصة جدًا"


def test_standard_leave_names_have_zh_translations():
    assert translate_text("إجازة مرضية", "zh") == "病假"


def test_audit_reason_localizes_known_app_generated():
    from app.audit_display import reason_label
    assert reason_label("employee_created", "Initial employee record created", "ar") == "أُنشئ سجل الموظف المبدئي"
    assert reason_label("employee_created", "Initial employee record created", "en") == "Initial employee record created"
    assert reason_label("employee_created", "Initial employee record created", "zh") == "初始员工记录已创建"
    assert reason_label("ai_command_expired", "انتهت صلاحية طلب الموافقة.", "en") == "The approval request expired."


def test_audit_reason_preserves_user_and_mismatched_reasons():
    from app.audit_display import reason_label
    assert reason_label("leave_cancelled", "Initial employee record created", "en") == "Initial employee record created"
    assert reason_label("employee_created", "سبب كتبه المستخدم", "en") == "سبب كتبه المستخدم"
    assert reason_label("employee_created", "", "en") == ""


def test_audit_detail_none_renders_as_dash():
    from app.audit_display import detail_label
    assert detail_label("code=X; department_id=None; branch_id=1", "en") == "Code=X; Department=—; Branch=1"


def test_extra_leave_type_names_have_zh_translations():
    assert translate_text("عارضة", "zh") == "事假"
    assert translate_text("راحة أسبوعية", "zh") == "每周休息"
    assert translate_text("عطلة رسمية", "zh") == "法定假日"


def test_audit_reason_pattern_with_runtime_value():
    from app.audit_display import reason_label
    assert reason_label("login_lockout", "2 failed login attempts", "ar") == "2 محاولة دخول فاشلة"
    assert reason_label("login_lockout", "2 failed login attempts", "en") == "2 failed login attempts"
    assert reason_label("login_lockout", "2 failed login attempts", "zh") == "2 次登录尝试失败"
    assert reason_label("login_lockout", "unrelated text", "en") == "unrelated text"


def test_official_seed_leave_types_render_true_zh():
    """Coverage derived from the official alembic 0016 seed rows, not fixtures."""
    import re as _re
    from pathlib import Path

    migration = Path("alembic/versions/0016_leave_management.py").read_text(encoding="utf-8")
    seeds = _re.findall(r'\{"code": "[^"]+", "name_ar": "([^"]+)", "name_en": "([^"]+)"', migration)
    assert len(seeds) == 6, f"expected the 6 official seed types, found {seeds}"
    for name_ar, name_en in seeds:
        item = SimpleNamespace(name_ar=name_ar, name_en=name_en)
        rendered = _leave_type_name(item, "zh")
        assert rendered not in (name_ar, name_en), (
            f"seed type {name_ar!r} must render true ZH, got {rendered!r}"
        )


def test_audit_detail_leave_record_pipe_payload():
    from app.audit_display import detail_label
    assert detail_label("2026-11-27|2026-11-28|2.00", "ar") == "من 27/11/2026 إلى 28/11/2026 · عدد الأيام: 2.00"
    assert detail_label("2026-11-27|2026-11-28|2.00", "en") == "From 27/11/2026 to 28/11/2026 · Days: 2.00"
    assert detail_label("2026-11-27|2026-11-28|2.00", "zh") == "从 27/11/2026 到 28/11/2026 · 天数：2.00"
    assert detail_label("a|b|c", "en") == "a|b|c"
