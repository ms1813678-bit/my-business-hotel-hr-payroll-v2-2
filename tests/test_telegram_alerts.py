"""Telegram alert transport: config, trilingual text, failure safety."""
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import telegram_alerts
from app.db import Base
from app.telegram_alerts import (
    build_alert,
    detect_chat_id,
    get_config,
    save_config,
    send_login_event,
    send_message,
    verify_token,
)


@pytest.fixture()
def db_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    yield factory
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def _fake_urlopen(payloads):
    class _Response:
        status = 200

        def __init__(self, body):
            self._body = body

        def read(self):
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    calls = []

    def _open(request, timeout=None):
        calls.append((request, timeout))
        url = request.full_url
        method = url.rsplit("/", 1)[-1]
        return _Response(json.dumps(payloads.get(method, {"ok": True, "result": {}})).encode("utf-8"))

    return calls, _open


def test_config_roundtrip_and_env_fallback(db_factory, monkeypatch):
    with db_factory.begin() as db:
        config = get_config(db)
        assert config["bot_token"] == "" and config["enabled"] and config["language"] == "ar"
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        config = get_config(db)
        assert config["bot_token"] == "env-token" and config["chat_id"] == "123"
        save_config(db, bot_token="db-token", chat_id="456", enabled=False, language="zh", actor="synthetic-admin")
        config = get_config(db)
        assert config["bot_token"] == "db-token"  # database wins over env
        assert config["chat_id"] == "456" and not config["enabled"] and config["language"] == "zh"
        save_config(db, bot_token="db-token", chat_id="456", enabled=True, language="fr", actor="synthetic-admin")
        assert get_config(db)["language"] == "ar"  # unsupported language falls back to Arabic


def test_trilingual_alert_text():
    ar = build_alert("login_lockout", "admin", "ar", failed_attempts=5, locked_minutes=120)
    assert "إيقاف الدخول" in ar and "admin" in ar and "120" in ar
    en = build_alert("login_success", "admin", "en")
    assert "successful sign-in" in en and "admin" in en
    zh = build_alert("login_lockout", "admin", "zh", failed_attempts=5, locked_minutes=120)
    assert "锁定" in zh and "120" in zh
    test_msg = build_alert("test", "", "en")
    assert "Test message" in test_msg


def test_send_message_and_verification(monkeypatch):
    calls, fake = _fake_urlopen({
        "getMe": {"ok": True, "result": {"username": "synthetic_hotel_bot"}},
        "getUpdates": {"ok": True, "result": [
            {"message": {"chat": {"id": 111}}},
            {"message": {"chat": {"id": 222}}},
        ]},
        "sendMessage": {"ok": True, "result": {}},
    })
    monkeypatch.setattr(telegram_alerts.urllib.request, "urlopen", fake)
    assert verify_token("tok") == "synthetic_hotel_bot"
    assert detect_chat_id("tok") == "222"  # latest chat wins
    assert send_message("tok", "222", "hello")
    assert calls[-1][0].full_url.endswith("/sendMessage")
    assert not send_message("", "222", "hello")  # missing config refuses cleanly


def test_failures_never_raise(monkeypatch):
    def _boom(request, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(telegram_alerts.urllib.request, "urlopen", _boom)
    assert verify_token("tok") is None
    assert detect_chat_id("tok") is None
    assert not send_message("tok", "1", "x")
    # The login event path must never break sign-in even with the DB unreachable.
    assert send_login_event("login_success", "admin") is False


def test_login_event_respects_config(db_factory, monkeypatch):
    calls, fake = _fake_urlopen({"sendMessage": {"ok": True, "result": {}}})
    monkeypatch.setattr(telegram_alerts.urllib.request, "urlopen", fake)

    import app.telegram_alerts as module
    import app.db as db_module
    monkeypatch.setattr(db_module, "SessionLocal", db_factory)

    with db_factory.begin() as db:
        save_config(db, bot_token="tok", chat_id="777", enabled=True, language="en", actor="synthetic-admin")
    assert module.send_login_event("login_success", "admin")
    sent = json.loads(calls[-1][0].data.decode("utf-8"))
    assert sent["chat_id"] == "777" and "successful sign-in" in sent["text"]

    with db_factory.begin() as db:
        save_config(db, bot_token="tok", chat_id="777", enabled=False, language="en", actor="synthetic-admin")
    calls.clear()
    assert module.send_login_event("login_success", "admin") is False
    assert calls == []  # disabled: nothing sent
