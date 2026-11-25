"""AI route errors: fixed per-language messages, never exception text. Synthetic, no DB, no model."""
import html
import http.client
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import ai_agent, ai_assistant, main
from app.csrf import csrf_protect
from app.dependencies import require_system_admin

LEAK = "SECRET-HOST db://user:pw@10.9.9.9/x سطر-داخلي"


class _Client:
    """Calls the route functions directly with a real starlette Request, so no middleware, DB or PostgreSQL is needed.
    Guards (CSRF, system-admin dependency) are FastAPI dependencies and are covered by the existing route tests."""
    def __init__(self): self.cookies = _Cookies()

    def post(self, path, data):
        from starlette.requests import Request
        lang = self.cookies.get("my_hr_language", "ar")
        req = Request({"type": "http", "method": "POST", "path": path, "headers": [], "query_string": b"",
                       "state": {"language": lang, "admin_role": "system_admin"}, "app": main.app,
                       "scheme": "http", "server": ("t", 80), "client": ("1.1.1.1", 1), "root_path": ""})
        admin = SimpleNamespace(username="synthetic", display_name="Synthetic", role="system_admin")
        token = data.get("token", "")
        if path == "/ai":
            resp = main.ai_ask(req, question=data["question"], admin=admin)
        elif path == "/ai/review":
            resp = main.ai_review(req, token=token, admin=admin)
        elif path == "/ai/approve":
            resp = main.ai_approve(req, token=token, review_receipt=data.get("review_receipt", ""),
                                   human_coverage_confirmed=data.get("human_coverage_confirmed", ""), admin=admin)
        else:
            resp = main.ai_reject(req, token=token, admin=admin)
        return SimpleNamespace(status_code=resp.status_code, text=bytes(resp.body).decode("utf-8"))


class _Cookies(dict):
    def set(self, k, v): self[k] = v


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main, "recent_memory", lambda *a: [])
    monkeypatch.setattr(main, "local_model_status", lambda *a: {"available": False, "message": "", "model": "Local"})
    monkeypatch.setattr(main, "build_smart_scan", lambda *a: [])
    monkeypatch.setattr(main, "get_csrf_token", lambda r: "t")
    return _Client()


def _boom(*a, **k):
    raise RuntimeError(LEAK)


def _text(resp):
    return html.unescape(resp.text)


def _has_ar(t): return any("\u0600" <= c <= "\u06ff" for c in t)
def _has_zh(t): return any("\u4e00" <= c <= "\u9fff" for c in t)


LANGS = ["ar", "en", "zh"]


def _msg_present(text, code, lang):
    return main._AI_ROUTE_ERRORS[code][lang] in text


@pytest.mark.parametrize("lang", LANGS)
@pytest.mark.parametrize("exc", [RuntimeError, ValueError])
def test_prepare_failure(client, monkeypatch, lang, exc):
    def boom(*a, **k): raise exc(LEAK)
    monkeypatch.setattr(main, "is_command_request", lambda q: True)
    monkeypatch.setattr(main, "prepare_command", boom)
    recorded = {}
    monkeypatch.setattr(main, "record_exchange", lambda u, q, a, language=None: recorded.update(a=a, language=language))
    client.cookies.set("my_hr_language", lang)
    r = client.post("/ai", data={"question": "x"})
    t = _text(r)
    assert recorded["language"] == lang and recorded["a"] == main._AI_ROUTE_ERRORS["prepare"][lang]
    assert r.status_code == 200 and _msg_present(t, "prepare", lang)
    assert "SECRET-HOST" not in t and "10.9.9.9" not in t and "سطر-داخلي" not in t
    assert "pending" not in t or "token" not in t


@pytest.mark.parametrize("lang", LANGS)
def test_hr_query_failure_and_recorded_language(client, monkeypatch, lang):
    recorded = {}
    class S:
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def scalar(s, *a, **k): return 0
    monkeypatch.setattr(main, "SessionLocal", lambda: S())
    monkeypatch.setattr(main, "is_command_request", lambda q: False)
    monkeypatch.setattr(main, "is_structured_hr_query", lambda q: True)
    monkeypatch.setattr(main, "answer_structured_hr_query", _boom)
    monkeypatch.setattr(main, "record_exchange", lambda u, q, a, language=None: recorded.update(a=a, language=language))
    client.cookies.set("my_hr_language", lang)
    r = client.post("/ai", data={"question": "x"})
    t = _text(r)
    assert _msg_present(t, "hr_query", lang) and "SECRET-HOST" not in t
    assert recorded["language"] == lang and "SECRET-HOST" not in recorded["a"]


@pytest.mark.parametrize("lang", LANGS)
def test_review_failure(client, monkeypatch, lang):
    monkeypatch.setattr(main, "review_command", _boom)
    client.cookies.set("my_hr_language", lang)
    t = _text(client.post("/ai/review", data={"token": "t"}))
    assert _msg_present(t, "review", lang) and "SECRET-HOST" not in t


@pytest.mark.parametrize("lang", LANGS)
def test_approve_failure_keeps_language_and_hides_detail(client, monkeypatch, lang):
    monkeypatch.setattr(main, "verify_review_receipt", lambda *a: True)
    monkeypatch.setattr(main, "approve_command", _boom)
    client.cookies.set("my_hr_language", lang)
    r = client.post("/ai/approve", data={"token": "t", "review_receipt": "r"})
    t = _text(r)
    assert _msg_present(t, "approve", lang) and "SECRET-HOST" not in t
    assert "الأصل بالعربية" not in t and "الأصل بالإنجليزية" not in t and "الأصل بالصينية" not in t  # no origin badge


@pytest.mark.parametrize("lang", LANGS)
def test_approve_without_receipt_still_blocked_and_localized(client, monkeypatch, lang):
    monkeypatch.setattr(main, "verify_review_receipt", lambda *a: False)
    monkeypatch.setattr(main, "approve_command", lambda *a, **k: pytest.fail("review bypass"))
    client.cookies.set("my_hr_language", lang)
    t = _text(client.post("/ai/approve", data={"token": "t"}))
    assert _msg_present(t, "review_required", lang)


@pytest.mark.parametrize("lang", LANGS)
def test_reject_failure(client, monkeypatch, lang):
    monkeypatch.setattr(main, "reject_command", _boom)
    client.cookies.set("my_hr_language", lang)
    t = _text(client.post("/ai/reject", data={"token": "t"}))
    assert _msg_present(t, "reject", lang) and "SECRET-HOST" not in t


@pytest.mark.parametrize("lang", LANGS)
def test_every_message_is_single_language(lang):
    for code, by in main._AI_ROUTE_ERRORS.items():
        t = by[lang]
        assert (_has_ar(t), _has_zh(t)) == {"ar": (True, False), "en": (False, False), "zh": (False, True)}[lang], code


def test_success_paths_unchanged(client, monkeypatch):
    monkeypatch.setattr(main, "verify_review_receipt", lambda *a: True)
    monkeypatch.setattr(main, "approve_command", lambda *a, **k: "Synthetic complete")
    monkeypatch.setattr(main, "reject_command", lambda *a, **k: "Synthetic rejected")
    assert "Synthetic complete" in client.post("/ai/approve", data={"token": "t", "review_receipt": "r"}).text
    assert "Synthetic rejected" in client.post("/ai/reject", data={"token": "t"}).text


# --- transport errors that used to escape as HTTP 500 ---
@pytest.mark.parametrize("exc", [http.client.IncompleteRead(b"ab", 5), http.client.RemoteDisconnected("x"),
                                 json.JSONDecodeError("m", "d", 0), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")])
def test_ask_local_model_transport_errors_become_runtimeerror(monkeypatch, exc):
    monkeypatch.setattr(ai_assistant, "local_model_status", lambda *a, **k: {"available": True, "model": "m", "message": ""})
    def raiser(*a, **k): raise exc
    monkeypatch.setattr(ai_assistant, "_post_json", raiser)
    with pytest.raises(RuntimeError) as e:
        ai_assistant.ask_local_model("hello there", {}, language="en")
    assert "Incomplete" not in str(e.value)
    with pytest.raises(RuntimeError):
        ai_assistant.ask_local_model_json("s", "u")


def test_json_plan_incompleteread_is_localized_runtimeerror(monkeypatch):
    monkeypatch.setattr(ai_agent, "local_model_status", lambda *a, **k: {"available": True, "model": "m", "message": ""})
    def raiser(*a, **k): raise http.client.IncompleteRead(b"ab", 5)
    monkeypatch.setattr(ai_assistant, "_post_json", raiser)
    with pytest.raises(RuntimeError) as e:
        ai_agent._json_plan("q", {}, "en")
    assert "did not respond" in str(e.value) and "IncompleteRead" not in str(e.value)


def test_local_model_status_incompleteread_means_unavailable(monkeypatch):
    class O:
        def open(self, *a, **k): raise http.client.IncompleteRead(b"", 1)
    monkeypatch.setattr(ai_assistant, "_pinned_opener", lambda: O())
    assert ai_assistant.local_model_status("en")["available"] is False


def test_ask_route_falls_back_when_model_stream_is_incomplete(client, monkeypatch):
    class S:
        def __enter__(s): return s
        def __exit__(s, *a): return False
        def scalar(s, *a, **k): return 0
    monkeypatch.setattr(main, "SessionLocal", lambda: S())
    monkeypatch.setattr(main, "is_command_request", lambda q: False)
    monkeypatch.setattr(main, "is_structured_hr_query", lambda q: False)
    monkeypatch.setattr(main, "record_exchange", lambda *a, **k: None)
    monkeypatch.setattr(ai_assistant, "local_model_status", lambda *a, **k: {"available": True, "model": "m", "message": ""})
    def raiser(*a, **k): raise http.client.IncompleteRead(b"ab", 5)
    monkeypatch.setattr(ai_assistant, "_post_json", raiser)
    assert client.post("/ai", data={"question": "hello"}).status_code == 200


# --- known business reasons stay visible, localized; unknown stay generic ---
from app.ai_errors import LocalizedValueError


@pytest.mark.parametrize("lang", LANGS)
def test_approve_known_reason_visible_localized(client, monkeypatch, lang):
    monkeypatch.setattr(main, "verify_review_receipt", lambda *a: True)
    def refuse(*a, **k): raise ValueError("Only a pending advance can be approved.")
    monkeypatch.setattr(main, "approve_command", refuse)
    client.cookies.set("my_hr_language", lang)
    t = _text(client.post("/ai/approve", data={"token": "t", "review_receipt": "r"}))
    from app import ai_error_display as d
    assert d.known_reason(ValueError("Only a pending advance can be approved."), lang) in t
    assert main._ai_route_error("approve", lang) not in t


from app.ai_errors import AUTHORED
_IDX = {"ar": 0, "en": 1, "zh": 2}


@pytest.mark.parametrize("lang", LANGS)
def test_approve_authored_gateway_refusal_in_request_language(client, monkeypatch, lang):
    monkeypatch.setattr(main, "verify_review_receipt", lambda *a: True)
    text = AUTHORED[3][_IDX[lang]]
    def refuse(*a, **k): raise LocalizedValueError(text, lang)
    monkeypatch.setattr(main, "approve_command", refuse)
    client.cookies.set("my_hr_language", lang)
    assert text in _text(client.post("/ai/approve", data={"token": "t", "review_receipt": "r"}))
    # forged marker: arbitrary text under the marker class is NOT rendered
    def forged(*a, **k): raise LocalizedValueError("SECRET-HOST forged", lang)
    monkeypatch.setattr(main, "approve_command", forged)
    t = _text(client.post("/ai/approve", data={"token": "t", "review_receipt": "r"}))
    assert "SECRET-HOST" not in t and _msg_present(t, "approve", lang)
    # authored in a different language than the request: hidden
    other = "en" if lang != "en" else "zh"
    def wrong(*a, **k): raise LocalizedValueError(AUTHORED[3][_IDX[other]], other)
    monkeypatch.setattr(main, "approve_command", wrong)
    t = _text(client.post("/ai/approve", data={"token": "t", "review_receipt": "r"}))
    assert AUTHORED[3][_IDX[other]] not in t and _msg_present(t, "approve", lang)


@pytest.mark.parametrize("lang", LANGS)
def test_reject_known_and_unknown(client, monkeypatch, lang):
    from app import ai_error_display as d
    monkeypatch.setattr(main, "reject_command", lambda *a, **k: (_ for _ in ()).throw(ValueError("This action is already cancelled.")))
    client.cookies.set("my_hr_language", lang)
    assert d.known_reason(ValueError("This action is already cancelled."), lang) in _text(client.post("/ai/reject", data={"token": "t"}))
    monkeypatch.setattr(main, "reject_command", _boom)
    t = _text(client.post("/ai/reject", data={"token": "t"}))
    assert _msg_present(t, "reject", lang) and "SECRET-HOST" not in t


@pytest.mark.parametrize("lang", LANGS)
def test_prepare_localized_model_refusal_visible(client, monkeypatch, lang):
    monkeypatch.setattr(main, "is_command_request", lambda q: True)
    text = next(item[_IDX[lang]] for item in AUTHORED if item[1].startswith("The requested command is not allowed"))
    def refuse(*a, **k): raise LocalizedValueError(text, lang)
    monkeypatch.setattr(main, "prepare_command", refuse)
    monkeypatch.setattr(main, "record_exchange", lambda *a, **k: None)
    client.cookies.set("my_hr_language", lang)
    assert text in _text(client.post("/ai", data={"question": "x"}))


def test_ai_answer_template_keeps_html_escaped():
    from starlette.requests import Request
    req = Request({"type":"http","method":"GET","path":"/ai","headers":[],"query_string":b"", "state":{"language":"en","admin_role":"system_admin"},"app":main.app,"scheme":"http","server":("t",80),"client":("127.0.0.1",1),"root_path":""})
    rendered = main.templates.env.get_template("ai.html").render(request=req, admin="Synthetic", csrf_token="synthetic", status={"available":False,"model":"Synthetic","message":""}, scan=[],question="",answer="<script>synthetic_alert()</script>",answer_language="en",pending=None,memory=[],ui_language="en")
    assert "<script>synthetic_alert()</script>" not in rendered
    assert "&lt;script&gt;synthetic_alert()&lt;/script&gt;" in rendered
