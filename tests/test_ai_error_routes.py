"""Authenticated AI route boundary on existing synthetic fixture, not real-model evidence."""
import pytest
from tests.test_routes_security import client, login, csrf_from  # noqa: F401

@pytest.mark.parametrize("command", [False, True])
def test_ai_transport_timeout_is_controlled_without_raw_error(monkeypatch, client, command):
    from app import main, ai_agent, ai_assistant
    assert login(client).status_code == 303
    monkeypatch.setattr(main, "is_command_request", lambda question: command)
    monkeypatch.setattr(main, "is_structured_hr_query", lambda question: False)
    monkeypatch.setattr("app.ai_hr_queries.is_structured_hr_query", lambda question: False)
    monkeypatch.setattr(ai_agent, "local_model_status", lambda *a, **k: {"available": True, "model": "Synthetic", "message": ""})
    monkeypatch.setattr(ai_assistant, "local_model_status", lambda *a, **k: {"available": True, "model": "Synthetic", "message": ""})
    monkeypatch.setattr(main, "local_model_status", lambda *a, **k: {"available": True, "model": "Synthetic", "message": ""})
    monkeypatch.setattr(ai_agent, "_plan_context", lambda db: {})
    def timeout(*a, **k):
        raise TimeoutError("synthetic-private-host:11434")
    monkeypatch.setattr(ai_assistant, "_post_json", timeout)
    page = client.get("/ai")
    response = client.post("/ai", data={"csrf_token": csrf_from(page), "question": "Explain a synthetic generic question"})
    assert response.status_code == 200
    assert "synthetic-private-host" not in response.text
    assert "TimeoutError" not in response.text
    assert 'action="/ai/approve"' not in response.text
