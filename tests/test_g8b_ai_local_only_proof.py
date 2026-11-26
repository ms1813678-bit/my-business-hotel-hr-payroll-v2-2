"""Group 8ب local-AI proof: loopback-only egress and absent-runtime refusal.

Isolated addition from the group-8 lane (2026-11-26), base commit
e60b73f269f3cf203c1cf7d86e437359970aa233. No shared file changed.

Additive to tests/test_ai_local_only.py (which exercises the private
transport): this module pins behavior at the PUBLIC entry points
(``local_model_status``, ``ask_local_model``, ``ai_agent._json_plan``) and
records raw ``socket.socket`` construction itself, so "refused before any
socket" is proven at the level the application actually calls.

Scope notes:
- Both AI network paths (assistant Q&A and command planning) funnel through
  app.ai_assistant._post_json, verified by reading app/ai_agent.py.
- A REAL local model (Ollama + installed weights) is NOT proven here: this
  test environment has no model runtime. This module proves the two states that can be
  proven without it: (a) with no runtime the assistant refuses cleanly and
  the rest of the system keeps working; (b) no configuration can turn the
  assistant into a non-loopback egress path.
"""
import socket
from unittest import mock

import pytest

import app.ai_assistant as ai
from app import ai_agent

HOSTILE_URLS = [
    "http://10.255.255.1:11434",          # RFC1918
    "http://203.0.113.9:11434",           # TEST-NET-3 public
    "http://127.0.0.1@evil.example/",     # userinfo confusion
    "http://127.1:11434",                 # short-form loopback
    "http://2130706433:11434",            # decimal loopback
    "http://[::ffff:127.0.0.1]:11434",    # v4-mapped
    "http://localhost.evil.example:11434",
    "file://localhost/x",
    "ftp://127.0.0.1/",
    "https://127.0.0.1.evil.example/",    # suffix confusion
]


@pytest.fixture()
def restore_base_url():
    old = ai.OLLAMA_BASE_URL
    yield
    ai.OLLAMA_BASE_URL = old


def _closed_loopback_url() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return f"http://127.0.0.1:{port}"


# --- (a) no local runtime: clean refusal, system keeps working --------------

def test_no_runtime_status_reports_unavailable_and_system_fine(restore_base_url):
    ai.OLLAMA_BASE_URL = _closed_loopback_url()
    status = ai.local_model_status("ar")
    assert status["available"] is False
    assert status["server"] is False
    assert status["model"]
    # The message must tell the operator the base system is unaffected.
    assert "النظام الأساسي يعمل بشكل طبيعي" in status["message"]
    # Deterministic help still answers with no model at all.
    answer = ai.local_fallback_answer("كيف أضيف موظفًا؟", {}, language="ar")
    assert isinstance(answer, str) and answer.strip()


def test_no_runtime_ask_and_command_plan_raise_without_model(restore_base_url):
    ai.OLLAMA_BASE_URL = _closed_loopback_url()
    with pytest.raises(RuntimeError):
        ai.ask_local_model("ما هو صافي رواتب الشهر؟", {}, language="ar")
    with pytest.raises(RuntimeError):
        ai_agent._json_plan("اعتمد مسير رواتب أكتوبر", {}, "ar")


def test_no_runtime_messages_localized(restore_base_url):
    ai.OLLAMA_BASE_URL = _closed_loopback_url()
    for language in ("ar", "en", "zh"):
        status = ai.local_model_status(language)
        assert status["available"] is False
        assert isinstance(status["message"], str) and status["message"].strip()


# --- (b) no configuration becomes a non-loopback egress path -----------------

@pytest.mark.parametrize("url", HOSTILE_URLS)
def test_hostile_base_url_refused_before_any_raw_socket(restore_base_url, url):
    ai.OLLAMA_BASE_URL = url
    sockets_made = []
    real_socket = socket.socket

    def recording_socket(*args, **kwargs):
        sockets_made.append(args)
        return real_socket(*args, **kwargs)

    with mock.patch.object(socket, "socket", side_effect=recording_socket):
        with pytest.raises(RuntimeError):
            ai._post_json("/api/chat", {"model": "m", "messages": []})
        status = ai.local_model_status()
        assert status["server"] is False
        with pytest.raises(RuntimeError):
            ai.ask_local_model("سؤال", {}, language="ar")
    assert sockets_made == []


def test_env_configured_external_url_still_refused(restore_base_url):
    """Even when LOCAL_AI_BASE_URL resolution yields an external host, the
    transport refuses: configuration cannot open an egress path."""
    from app.ai_local_config import resolve_base_url

    resolved = resolve_base_url(env={"LOCAL_AI_BASE_URL": "http://203.0.113.9:11434"})
    assert resolved == "http://203.0.113.9:11434"  # config resolves verbatim...
    ai.OLLAMA_BASE_URL = resolved
    sockets_made = []
    with mock.patch.object(socket, "socket", side_effect=AssertionError("socket used")):
        with pytest.raises(RuntimeError):
            ai._post_json("/api/chat", {})
        assert ai.local_model_status()["server"] is False
    assert sockets_made == []


def test_dns_rebinding_refused_before_any_raw_socket(restore_base_url):
    """'localhost' resolving off-machine: resolution happens (mocked), the
    answer is rejected, and no raw socket is ever constructed."""
    ai.OLLAMA_BASE_URL = "http://localhost:11434"
    fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 11434))]
    with mock.patch.object(socket, "getaddrinfo", return_value=fake), \
            mock.patch.object(socket, "socket", side_effect=AssertionError("socket used")):
        with pytest.raises(RuntimeError):
            ai._post_json("/api/chat", {})
        assert ai.local_model_status()["server"] is False
