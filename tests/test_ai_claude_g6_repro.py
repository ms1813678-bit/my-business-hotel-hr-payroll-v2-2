"""Repro tests for externally reported local-AI defects D1-D6 (current behavior is asserted as the DESIRED behavior)."""
import json
import types
import pytest
from app import ai_agent, ai_assistant, ai_gateway
from app.ai_plan_validation import validate_plan_values


@pytest.mark.parametrize("action", [["approve_advance"], {}])
def test_d1_non_string_action_is_valueerror(action):
    with pytest.raises(ValueError):
        validate_plan_values({"action": action})


def test_d1_json_plan_non_string_action_valueerror(monkeypatch):
    monkeypatch.setattr(ai_agent, "local_model_status", lambda *a, **k: {"available": True, "model": "m", "message": ""})
    monkeypatch.setattr(ai_assistant, "_post_json", lambda *a, **k: {"message": {"content": json.dumps({"action": ["x"]})}})
    with pytest.raises(ValueError):
        ai_agent._json_plan("q", {}, "en")


def test_d2_incomplete_plan_rejected():
    with pytest.raises(ValueError):
        validate_plan_values({"action": "confirm_leave"})


def test_d2_edit_employee_null_department():
    class E:
        id = 1; department_id = 1; branch_id = None; nationality = "egyptian"; national_id = None
        full_name = "X"; position = "p"; basic_salary = 1; daily_hours = 8; phone = None; address = None; fingerprint_id = None; active = True
    class DB:
        def get(self, m, pk): return E()
        def add(self, x): pass
    # null department/branch in an edit means "unchanged"; must not raise TypeError
    result = ai_agent.execute_plan({"action": "edit_employee", "employee_id": 1, "full_name": "X",
                                    "department_id": None, "branch_id": None}, "a", db=DB())
    assert result.key == "edit_employee"


def test_d3_timeout_becomes_runtimeerror(monkeypatch):
    monkeypatch.setattr(ai_assistant, "local_model_status", lambda *a, **k: {"available": True, "model": "m", "message": ""})
    def boom(*a, **k): raise TimeoutError()
    monkeypatch.setattr(ai_assistant, "_post_json", boom)
    with pytest.raises(RuntimeError):
        ai_assistant.ask_local_model("hello there", {})


def test_d3_json_plan_timeout(monkeypatch):
    monkeypatch.setattr(ai_agent, "local_model_status", lambda *a, **k: {"available": True, "model": "m", "message": ""})
    def boom(*a, **k): raise TimeoutError()
    monkeypatch.setattr(ai_assistant, "_post_json", boom)
    with pytest.raises((RuntimeError, ValueError)):
        ai_agent._json_plan("q", {}, "en")


def test_d4_non_ascii_receipt(monkeypatch):
    rec = types.SimpleNamespace(actor="a", plan_hash="h")
    class S:
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def scalar(s, *a, **k): return rec
    monkeypatch.setattr(ai_gateway, "SessionLocal", lambda: S())
    monkeypatch.setattr(ai_gateway, "review_receipt", lambda *a: "0" * 64)
    assert ai_gateway.verify_review_receipt("t", "a", "\u0639") is False




def test_d6_whitespace_reason_is_missing():
    with pytest.raises(ValueError):
        validate_plan_values({"action": "reopen_payroll_run", "payroll_run_id": 3, "reason": "  "})


def test_d5_fingerprint_id_is_text_like_the_employee_form():
    validate_plan_values({"action": "edit_employee", "employee_id": 1, "fingerprint_id": "0042"})
    validate_plan_values({"action": "edit_employee", "employee_id": 1, "fingerprint_id": "FP-1001"})
    validate_plan_values({"action": "edit_employee", "employee_id": 1, "fingerprint_id": None})


@pytest.mark.parametrize("value", [42, True, 4.2, "", "   ", "x" * 51, ["1"]])
def test_d5_fingerprint_id_non_text_rejected(value):
    with pytest.raises(ValueError):
        validate_plan_values({"action": "edit_employee", "employee_id": 1, "fingerprint_id": value})


# Required keys = unguarded plan["key"] reads in execute_plan at 08bfbb5 (a missing key is a KeyError at approval time).
READ_UNGUARDED = {
    "create_employee": ["department_id"],
    "edit_employee": ["employee_id"],
    "transfer_employee": ["employee_id", "department_id"],
    "archive_employee": ["employee_id"],
    "rehire_employee": ["employee_id"],
    "create_leave": ["employee_id", "leave_type_id", "start_date", "end_date", "charged_days"],
    "confirm_leave": ["record_id"],
    "cancel_leave": ["record_id"],
    "delete_draft_leave": ["record_id"],
    "adjust_leave_balance": ["employee_id", "leave_type_id", "balance_days"],
    "create_advance": ["employee_id", "amount"],
}
_VALUES = {"department_id": 1, "employee_id": 1, "leave_type_id": 1, "record_id": 1, "balance_days": "1",
           "amount": "10", "start_date": "2026-01-01", "end_date": "2026-01-02", "charged_days": "1"}


@pytest.mark.parametrize("action,key", [(a, k) for a, ks in READ_UNGUARDED.items() for k in ks])
def test_d2_missing_required_key_rejected_and_complete_plan_accepted(action, key):
    full = {"action": action, **{k: _VALUES[k] for k in READ_UNGUARDED[action]}}
    validate_plan_values(full)
    broken = dict(full); broken.pop(key)
    with pytest.raises(ValueError):
        validate_plan_values(broken)
    broken = dict(full); broken[key] = None
    with pytest.raises(ValueError):
        validate_plan_values(broken)


def test_edit_preview_omits_null_fields():
    from app import ai_gateway
    emp = types.SimpleNamespace(id=1, code="E1", full_name="A", status="active", department_id=1, branch_id=None,
                                position="p", hire_date=None, basic_salary=1, daily_hours=8, phone=None,
                                address=None, national_id=None, fingerprint_id=None)
    class S:
        def scalar(self, *a, **k): return emp
    out = ai_gateway.build_confirmation_preview(
        {"action": "edit_employee", "employee_id": 1, "full_name": "B", "department_id": None, "basic_salary": None},
        session=S())
    assert out["new_values"] == {"full_name": "B"}
    assert not any("الراتب" in r for r in out["risks"])


@pytest.mark.parametrize("receipt", ["\ud800" * 64, "\u0639", "A" * 64, "a" * 63, "a" * 65, "", None, 5, b"a" * 64])
def test_d4_malformed_receipt_fails_closed_before_db(monkeypatch, receipt):
    def no_db():
        raise AssertionError("DB must not be touched for a malformed receipt")
    monkeypatch.setattr(ai_gateway, "SessionLocal", no_db)
    assert ai_gateway.verify_review_receipt("t", "a", receipt) is False


def test_d4_valid_receipt_still_verifies(monkeypatch):
    rec = types.SimpleNamespace(actor="a", plan_hash="h")
    class S:
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def scalar(s, *a, **k): return rec
    monkeypatch.setattr(ai_gateway, "SessionLocal", lambda: S())
    good = "ab" * 32
    monkeypatch.setattr(ai_gateway, "review_receipt", lambda *a: good)
    assert ai_gateway.verify_review_receipt("t", "a", good) is True
    assert ai_gateway.verify_review_receipt("t", "b", good) is False
    monkeypatch.setattr(ai_gateway, "review_receipt", lambda *a: "cd" * 32)
    assert ai_gateway.verify_review_receipt("t", "a", good) is False


@pytest.mark.parametrize("reply", [{}, {"message": None}, {"message": "x"}, {"message": []}, []])
def test_d3_odd_reply_shapes_are_runtimeerror(monkeypatch, reply):
    monkeypatch.setattr(ai_assistant, "local_model_status", lambda *a, **k: {"available": True, "model": "m", "message": ""})
    monkeypatch.setattr(ai_assistant, "_post_json", lambda *a, **k: reply)
    with pytest.raises(RuntimeError):
        ai_assistant.ask_local_model("hello there", {})
    with pytest.raises(RuntimeError):
        ai_assistant.ask_local_model_json("s", "u")


@pytest.mark.parametrize("lang", ["ar", "en", "zh"])
def test_d3_timeout_message_has_no_internals_and_follows_language(monkeypatch, lang):
    monkeypatch.setattr(ai_agent, "local_model_status", lambda *a, **k: {"available": True, "model": "m", "message": ""})
    monkeypatch.setattr(ai_assistant, "_post_json", lambda *a, **k: (_ for _ in ()).throw(TimeoutError("secret-host:11434")))
    with pytest.raises(RuntimeError) as exc:
        ai_agent._json_plan("q", {}, lang)
    assert "secret-host" not in str(exc.value) and "11434" not in str(exc.value)
    has_ar = any("\u0600" <= c <= "\u06ff" for c in str(exc.value))
    has_zh = any("\u4e00" <= c <= "\u9fff" for c in str(exc.value))
    assert {"ar": has_ar and not has_zh, "en": not has_ar and not has_zh, "zh": has_zh and not has_ar}[lang]


@pytest.mark.parametrize("reply", [[], None, "x", 5])
def test_json_plan_non_dict_toplevel_is_controlled_refusal(monkeypatch, reply):
    monkeypatch.setattr(ai_agent, "local_model_status", lambda *a, **k: {"available": True, "model": "m", "message": ""})
    monkeypatch.setattr(ai_assistant, "_post_json", lambda *a, **k: reply)
    with pytest.raises(ValueError) as exc:
        ai_agent._json_plan("q", {}, "en")
    assert "valid JSON command" in str(exc.value)
    with pytest.raises(ValueError) as exc:
        ai_agent._json_plan("q", {}, "zh")
    assert "JSON" in str(exc.value) and not any("\u0600" <= c <= "\u06ff" for c in str(exc.value))


def test_json_plan_undecodable_transport_body_is_controlled_refusal(monkeypatch):
    monkeypatch.setattr(ai_agent, "local_model_status", lambda *a, **k: {"available": True, "model": "m", "message": ""})
    def bad(*a, **k): raise json.JSONDecodeError("secret-internal", "x", 0)
    monkeypatch.setattr(ai_assistant, "_post_json", bad)
    with pytest.raises(ValueError) as exc:
        ai_agent._json_plan("q", {}, "en")
    assert "secret-internal" not in str(exc.value)


class _Emp(types.SimpleNamespace):
    pass


def _edit_emp():
    return _Emp(id=1, code="E1", full_name="A", status="active", department_id=1, branch_id=None, position="p",
                hire_date=None, basic_salary=1, daily_hours=8, phone=None, address=None, national_id=None,
                nationality="egyptian", fingerprint_id="OLD")


class _DB:
    def __init__(self, emp): self.emp = emp; self.added = []
    def get(self, model, pk): return self.emp
    def add(self, obj): self.added.append(obj)
    def scalar(self, *a, **k): return self.emp


@pytest.mark.parametrize("raw,stored", [("  0042  ", "0042"), ("FP-1001", "FP-1001"), (" " + "x" * 50 + " ", "x" * 50)])
def test_edit_fingerprint_preview_and_execution_agree(monkeypatch, raw, stored):
    plan = {"action": "edit_employee", "employee_id": 1, "fingerprint_id": raw}
    emp = _edit_emp(); db = _DB(emp)
    monkeypatch.setattr(ai_agent, "EmployeeChange", lambda **k: types.SimpleNamespace(**k))
    monkeypatch.setattr(ai_agent, "AuditLog", lambda **k: types.SimpleNamespace(**k))
    ai_agent.execute_plan(plan, "admin", db=db)
    assert emp.fingerprint_id == stored
    preview = ai_gateway.build_confirmation_preview(plan, session=db)
    assert preview["new_values"] == {"fingerprint_id": stored}


def test_fingerprint_over_50_after_strip_rejected_and_blank_rejected():
    for bad in ("x" * 51, " " + "x" * 51 + " ", "   ", ""):
        with pytest.raises(ValueError):
            validate_plan_values({"action": "edit_employee", "employee_id": 1, "fingerprint_id": bad})
    validate_plan_values({"action": "edit_employee", "employee_id": 1, "fingerprint_id": " " + "x" * 50 + " "})


@pytest.mark.parametrize("language", ["ar", "en", "zh"])
def test_unavailable_plan_model_uses_requested_language(monkeypatch, language):
    seen = []
    def status(lang="ar"):
        seen.append(lang)
        return {"available": False, "message": "Unavailable:" + lang}
    monkeypatch.setattr(ai_agent, "local_model_status", status)
    with pytest.raises(RuntimeError, match="Unavailable:" + language):
        ai_agent._json_plan("q", {}, language)
    assert seen == [language]
