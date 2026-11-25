"""Known approve/reject refusals stay visible, localized; everything else is generic. Synthetic, no DB, no model."""
import ast
import pathlib

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app import ai_error_display as d
from app.ai_errors import LocalizedRuntimeError, LocalizedValueError
from app.payroll_workflow import AbsenceLeaveConflictError

ROOT = pathlib.Path(__file__).resolve().parents[1]
# module -> entry functions reachable from ai_agent.execute_plan / the AI approval path
ENTRY = {
    "leave_balance_rules": ["validate_manual_balance_change"],
    "advance_workflow": ["approve_advance", "create_advance", "mark_advance_paid", "reject_advance"],
    "leave_workflow": ["add_balance_entry", "cancel_leave", "confirm_leave", "leave_balance"],
    "attendance_workflow": ["add_manual_attendance", "approve_attendance_day", "approve_attendance_month"],
    "payroll_workflow": ["approve_payroll_run", "close_payroll_run", "reopen_payroll_run"],
    "staff_actions_workflow": ["approve_staff_action", "cancel_staff_action"],
    "ai_agent": ["execute_plan"],
}
# Static messages deliberately NOT shown (internal/operator facts): they get the generic text.
GENERIC_ON_PURPOSE = {"Human confirmation must be explicit boolean."}


def _static_raise_messages():
    found = {}
    for module, names in ENTRY.items():
        tree = ast.parse((ROOT / "app" / f"{module}.py").read_text(encoding="utf-8"))
        funcs = {f.name: f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)}
        done, stack = set(), list(names)
        while stack:
            name = stack.pop()
            if name in done or name not in funcs:
                continue
            done.add(name)
            for node in ast.walk(funcs[name]):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in funcs:
                    stack.append(node.func.id)
                if isinstance(node, ast.Dict):  # leave-draft code -> message map in execute_plan
                    for v in node.values:
                        if isinstance(v, ast.Constant) and isinstance(v.value, str) and any("\u0600" <= c <= "\u06ff" for c in v.value):
                            found[v.value] = (module, name)
                if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args:
                    fn = node.exc.func
                    if isinstance(fn, ast.Name) and fn.id in {"LeaveDraftError", "AbsenceLeaveConflictError"}:
                        continue
                    arg = node.exc.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        found[arg.value] = (module, name)
    return found


def test_every_static_message_on_the_approval_path_is_mapped_or_explicitly_generic():
    missing = {m: where for m, where in _static_raise_messages().items()
               if m not in d._REASONS and m not in GENERIC_ON_PURPOSE and m not in {x for x in [d.ERROR_MESSAGE]}
               and not m.startswith(("Employee code counter is missing",))}
    assert not missing, f"unmapped approval-path messages (add to ai_error_display._REASONS): {missing}"


@pytest.mark.parametrize("key", sorted(d._REASONS))
def test_each_mapped_reason_is_single_language_in_each_ui_language(key):
    entry = d._REASONS[key]
    has_ar = lambda t: any("\u0600" <= c <= "\u06ff" for c in t)
    has_zh = lambda t: any("\u4e00" <= c <= "\u9fff" for c in t)
    ar, en, zh = (d._pick(entry, key, lang) for lang in ("ar", "en", "zh"))
    assert has_ar(ar) and not has_zh(ar)
    assert en.strip() and not has_ar(en) and not has_zh(en)
    assert has_zh(zh) and not has_ar(zh)
    exc = ValueError(key)
    assert d.known_reason(exc, "ar") == ar and d.known_reason(exc, "en") == en and d.known_reason(exc, "zh") == zh


@pytest.mark.parametrize("lang", ["ar", "en", "zh"])
def test_unknown_and_internal_text_is_generic(lang):
    for exc in (ValueError("SECRET-HOST db://u:p@10.0.0.1/x"), RuntimeError("connection refused 127.0.0.1:11434"),
                SQLAlchemyError("(psycopg) relation foo"), ValueError("Human confirmation must be explicit boolean."),
                ValueError("Only a pending advance can be approved. extra"), KeyError("employee_id"),
                ValueError("a", "b"), ValueError()):
        assert d.safe_failure_message(exc, "P", "GENERIC", lang) == "GENERIC"


@pytest.mark.parametrize("lang", ["ar", "en", "zh"])
def test_known_reason_gets_prefix(lang):
    msg = d.safe_failure_message(ValueError("Only a pending advance can be approved."), "PFX", "GENERIC", lang)
    assert msg.startswith("PFX: ") and msg != "GENERIC"


@pytest.mark.parametrize("lang", ["ar", "en", "zh"])
def test_parameterised_reasons(lang):
    cases = [
        "Monthly advance limit is 2500.00; already reserved: 100.00.",
        "Payroll deductions exceed earnings for employee #12.",
        "Advance deductions changed after payroll draft for employee #7; reopen/recreate payroll before closing.",
        "national_id مستخدم بالفعل.", "fingerprint_id مستخدم بالفعل.",
        "التاريخ البداية غير صالح؛ استخدم YYYY-MM-DD.", "القيمة الأيام غير رقمية.",
    ]
    for text in cases:
        assert d.known_reason(ValueError(text), lang), text
    assert "E001" in d.known_reason(AbsenceLeaveConflictError(["E001", "E002"]), lang)
    # a pattern must not swallow trailing/embedded extra text
    assert d.known_reason(ValueError("Payroll deductions exceed earnings for employee #12. host=10.0.0.1"), lang) is None
    assert d.known_reason(ValueError("national_id مستخدم بالفعل. secret"), lang) is None


def test_national_id_error_is_localized():
    from app.national_id import ERROR_MESSAGE
    en = d.known_reason(ValueError(ERROR_MESSAGE), "en")
    assert "14 digits" in en and not any("\u0600" <= c <= "\u06ff" for c in en)


def test_localized_message_requires_exact_catalog_text_and_language():
    from app.ai_errors import AUTHORED
    ar, en, zh = AUTHORED[11]
    exc = LocalizedValueError(en, "en")
    assert d.known_reason(exc, "en") == en
    assert d.known_reason(exc, "zh") is None and d.known_reason(exc, "ar") is None
    assert d.safe_failure_message(exc, "P", "G", "en") == en
    assert d.safe_failure_message(LocalizedRuntimeError(en, "en"), "P", "G", "zh") == "G"
    # language/text mismatch: zh text declared as en
    assert d.known_reason(LocalizedValueError(zh, "en"), "en") is None


@pytest.mark.parametrize("lang", ["ar", "en", "zh"])
def test_forged_mutated_and_malformed_markers_are_not_trusted(lang):
    from app.ai_errors import AUTHORED
    good = AUTHORED[11][{"ar": 0, "en": 1, "zh": 2}[lang]]
    # forged: marker class with arbitrary/internal text
    assert d.known_reason(LocalizedValueError("SECRET-HOST db://u:p@10.0.0.1/x", lang), lang) is None
    assert d.known_reason(LocalizedRuntimeError("connection refused", lang), lang) is None
    # catalog text plus extra text, or a near match
    assert d.known_reason(LocalizedValueError(good + " host=10.0.0.1", lang), lang) is None
    assert d.known_reason(LocalizedValueError(good[:-1], lang), lang) is None
    # mutated after construction
    e = LocalizedValueError(good, lang); e.args = ("SECRET-HOST mutated",)
    assert d.known_reason(e, lang) is None
    e = LocalizedValueError(good, lang); e.language = "xx"
    assert d.known_reason(e, lang) is None
    e = LocalizedValueError(good, lang); e.language = None
    assert d.known_reason(e, lang) is None
    # extra args / wrong types
    e = LocalizedValueError(good, lang); e.args = (good, "SECRET")
    assert d.known_reason(e, lang) is None
    e = LocalizedValueError(good, lang); e.args = (b"x",)
    assert d.known_reason(e, lang) is None
    e = LocalizedValueError(good, lang); e.args = ()
    assert d.known_reason(e, lang) is None
    # str subclass whose text differs from its catalogued value
    class S(str):
        def __eq__(self, o): return True
        __hash__ = str.__hash__
    e = LocalizedValueError(good, lang); e.args = (S("SECRET"),)
    assert d.known_reason(e, lang) is None
    # subclass of the typed class is not trusted
    class Sub(LocalizedValueError): pass
    assert d.known_reason(Sub(good, lang), lang) is None
    # a plain ValueError carrying catalog text still is not a business refusal
    assert d.known_reason(ValueError(good), lang) is None
    # a bare marker subclass of Exception is not trusted either
    class Evil(Exception, d.LocalizedMessage):
        language = lang
    assert d.known_reason(Evil(good), lang) is None


def test_every_localized_raise_site_text_is_in_the_catalog():
    from app.ai_errors import AUTHORED
    flat = {s for item in AUTHORED for s in item}
    triples = {item for item in AUTHORED}
    sites = (("ai_gateway", {"approve_command", "reject_command", "review_command"}),
             ("ai_agent", {"_json_plan", "_bad_json_msg"}))
    seen = 0
    for module, funcs in sites:
        tree = ast.parse((ROOT / "app" / f"{module}.py").read_text(encoding="utf-8"))
        for fn in (f for f in ast.walk(tree) if isinstance(f, ast.FunctionDef) and f.name in funcs):
            for node in ast.walk(fn):
                if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "_msg" and len(node.args) == 4:
                    lits = tuple(a.value for a in node.args[1:] if isinstance(a, ast.Constant))
                    if len(lits) == 3 and not any(lits[1].startswith(p) for p in ("The operation was rejected",)):
                        assert lits in triples, (module, fn.name, node.lineno, lits[1])
                        seen += 1
    assert seen >= 14
    from app.ai_plan_validation import validate_plan_values
    for lang in ("ar", "en", "zh"):
        with pytest.raises(ValueError) as e:
            validate_plan_values({"action": 5}, lang)
        assert str(e.value) in flat and d.known_reason(e.value, lang) == str(e.value)
    assert all(len(set(item)) == 3 for item in AUTHORED)


@pytest.mark.parametrize("text", [
    "Monthly advance limit is ٢٥٠٠; already reserved: ٠.",
    "Monthly advance limit is " + "9" * 13 + "; already reserved: 1.",
    "Monthly advance limit is 1.23456; already reserved: 1.",
    "Payroll deductions exceed earnings for employee #" + "1" * 11 + ".",
    "Payroll deductions exceed earnings for employee #١٢.",
    "Payroll deductions exceed earnings for employee #12.\n",
    "Unreviewed absence/paid-leave conflicts: E١",
    "Unreviewed absence/paid-leave conflicts: " + ", ".join(["E1"] * 101),
    "Unreviewed absence/paid-leave conflicts: " + "X" * 41,
    "التاريخ " + "ب" * 21 + " غير صالح؛ استخدم YYYY-MM-DD.",
    "التاريخ x غير صالح؛ استخدم YYYY-MM-DD.",
    "القيمة internal-host غير رقمية.",
])
def test_pattern_captures_are_ascii_bounded_and_strict(text):
    for lang in ("ar", "en", "zh"):
        assert d.known_reason(ValueError(text), lang) is None, text


def test_overlong_text_is_rejected_before_matching():
    assert d.known_reason(ValueError("Only a pending advance can be approved." + " " * 500), "en") is None


def test_exception_subclasses_and_non_str_args_are_not_business_reasons():
    class V(ValueError): pass
    assert d.known_reason(V("Only a pending advance can be approved."), "en") is None
    assert d.known_reason(ValueError(b"Only a pending advance can be approved."), "en") is None


def test_real_validation_error_is_localized_typed():
    from app.ai_plan_validation import validate_plan_values
    with pytest.raises(ValueError) as e:
        validate_plan_values({"action": "edit_employee", "employee_id": "x"}, "en")
    assert isinstance(e.value, LocalizedValueError) and e.value.language == "en"
    assert d.known_reason(e.value, "en") == str(e.value)


@pytest.mark.parametrize('lang', ['ar','en','zh'])
def test_malformed_manual_opening_receipt_refusal_is_localized(lang):
    key = 'Malformed opening receipt evidence.'
    text = d.safe_failure_message(ValueError(key), 'PFX', 'GENERIC', lang)
    assert text == 'PFX: ' + d.known_reason(ValueError(key), lang)
    assert text != 'GENERIC'
    assert d.known_reason(ValueError(key), lang) == d._pick(d._REASONS[key], key, lang)
