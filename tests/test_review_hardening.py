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
