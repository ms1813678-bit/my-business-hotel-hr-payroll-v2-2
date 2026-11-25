"""Local-model plan proposal contract, checked with a fake local Ollama server.

Fixture evidence only: scripted replies, no real model, no claim about how often
any real model produces these shapes. Proves the request asks for Ollama JSON mode
and that non-strict, malformed or unsafe replies are rejected by the existing
parser, allowlist and value validation.
"""
import http.server
import json
import threading

import pytest

from app import ai_agent, ai_assistant


class FakeModel:
    def __init__(self, content):
        self.content = content
        self.requests = []
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, obj):
                b = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                self._reply({"models": [{"name": ai_assistant.OLLAMA_MODEL}]})

            def do_POST(self):
                outer.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self._reply({"message": {"role": "assistant", "content": outer.content}})

        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def model(monkeypatch):
    made = []

    def make(content):
        m = FakeModel(content)
        made.append(m)
        monkeypatch.setattr(ai_assistant, "OLLAMA_BASE_URL", f"http://127.0.0.1:{m.port}")
        return m

    monkeypatch.setattr(ai_agent, "_plan_context", lambda db: {})

    class _S:
        def __enter__(self):
            return None

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ai_agent, "SessionLocal", lambda: _S())
    yield make
    for m in made:
        m.close()


GOOD = {"action": "approve_advance", "advance_id": 7, "installment_count": None,
        "summary": "اعتماد السلفة"}


def plan(question="اعتمد السلفة 7", lang="ar"):
    return ai_agent.build_plan(question, language=lang)


def test_clean_json_is_accepted(model):
    model(json.dumps(GOOD, ensure_ascii=False))
    assert plan()["advance_id"] == 7


def test_request_asks_ollama_for_json_mode(model):
    m = model(json.dumps(GOOD))
    plan()
    assert m.requests[0].get("format") == "json"
    assert m.requests[0]["stream"] is False


@pytest.mark.parametrize("wrap", [
    "```json\n{j}\n```",
    "Here is the command:\n{j}",
    "{j}\nLet me know if you need anything else.",
])
def test_wrapped_replies_stay_rejected_strict_json(model, wrap):
    """Strict parsing is intentional: no heuristic extraction from prose."""
    model(wrap.replace("{j}", json.dumps(GOOD)))
    with pytest.raises(ValueError):
        plan()


@pytest.mark.parametrize("content", [
    "", "I cannot do that.", "[]", "[" + json.dumps(GOOD) + "]", "null", "7",
    json.dumps({"action": "run_sql", "sql": "DROP TABLE employees"}),
    json.dumps({"action": "approve_advance; DROP TABLE x"}),
    json.dumps({"action": None}),
    json.dumps({"advance_id": 7}),
    json.dumps({"action": "approve_advance", "advance_id": "7"}),
    json.dumps({"action": "approve_advance", "advance_id": -1}),
    json.dumps({"action": "approve_advance", "advance_id": True}),
    json.dumps({"action": "approve_advance", "advance_id": 7.0}),
    json.dumps({"action": "create_advance", "employee_id": 1, "amount": "NaN"}),
    json.dumps({"action": "create_advance", "employee_id": 1, "amount": "Infinity"}),
    json.dumps({"action": "approve_payroll_run"}),
    json.dumps({"action": "reopen_payroll_run", "payroll_run_id": 3}),
    json.dumps({"action": "create_staff_action", "employee_id": 1, "kind": "reward",
                "category": "x", "title": "t", "is_financial": "yes"}),
])
def test_unsafe_or_malformed_proposals_are_rejected(model, content):
    model(content)
    with pytest.raises((ValueError, RuntimeError)):
        plan()


def test_two_json_objects_are_not_merged(model):
    other = {"action": "approve_payroll_run", "payroll_run_id": 1}
    model(json.dumps(GOOD) + "\n" + json.dumps(other))
    with pytest.raises((ValueError, RuntimeError)):
        plan()


def test_rejection_message_follows_language(model):
    model("not json")
    with pytest.raises(ValueError) as en:
        plan(lang="en")
    assert not any("\u0600" <= c <= "\u06ff" for c in str(en.value))
    with pytest.raises(ValueError) as zh:
        plan(lang="zh")
    assert any("\u4e00" <= c <= "\u9fff" for c in str(zh.value))


def test_prompt_stays_local_and_carries_only_context_not_secrets(model):
    m = model(json.dumps(GOOD))
    plan()
    blob = json.dumps(m.requests[0], ensure_ascii=False)
    for forbidden in ("SESSION_SECRET", "DATABASE_URL", "password"):
        assert forbidden.lower() not in blob.lower()
