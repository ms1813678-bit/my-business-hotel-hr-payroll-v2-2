"""Telegram alert transport: config, trilingual text, nonce binding, failure safety."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import telegram_alerts
from app.db import Base
from app.telegram_alerts import (
    bind_chat_from_nonce,
    bind_deep_link,
    build_alert,
    generate_bind_nonce,
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


def _updates(*messages):
    return {"ok": True, "result": [{"message": m} for m in messages]}


def _private(chat_id, text):
    return {"chat": {"id": chat_id, "type": "private"}, "text": text}


def test_config_roundtrip_and_env_fallback(db_factory, monkeypatch):
    with db_factory.begin() as db:
        config = get_config(db)
        assert config["bot_token"] == "" and config["enabled"] and config["language"] == "ar"
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
        config = get_config(db)
        assert config["bot_token"] == "env-token" and config["chat_id"] == "123"
        save_config(db, bot_token="db-token", chat_id="456", enabled=False, language="zh",
                    actor="synthetic-admin", bot_username="synthetic_hotel_bot")
        config = get_config(db)
        assert config["bot_token"] == "db-token"  # database wins over env
        assert config["chat_id"] == "456" and not config["enabled"] and config["language"] == "zh"
        assert config["bot_username"] == "synthetic_hotel_bot"
        save_config(db, bot_token="db-token", chat_id="456", enabled=True, language="fr", actor="synthetic-admin")
        assert get_config(db)["language"] == "ar"  # unsupported language falls back to Arabic


def test_save_config_preserves_binding_and_clear_on_token_change(db_factory):
    with db_factory.begin() as db:
        save_config(db, bot_token="tok", chat_id="", enabled=True, language="ar", actor="synthetic-admin")
        nonce = generate_bind_nonce(db, actor="synthetic-admin")
        assert nonce
        # Ordinary save keeps the pending binding state.
        save_config(db, bot_token="tok", chat_id="", enabled=False, language="ar", actor="synthetic-admin")
        assert get_config(db)["bind_nonce"] == nonce
        # Changing the bot invalidates the old chat binding.
        save_config(db, bot_token="tok2", chat_id="", enabled=True, language="ar",
                    actor="synthetic-admin", clear_binding=True)
        config = get_config(db)
        assert config["bind_nonce"] == "" and config["chat_id"] == "" and not config["bind_nonce_used"]


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
        "sendMessage": {"ok": True, "result": {}},
    })
    monkeypatch.setattr(telegram_alerts.urllib.request, "urlopen", fake)
    assert verify_token("tok") == "synthetic_hotel_bot"
    assert send_message("tok", "222", "hello")
    assert calls[-1][0].full_url.endswith("/sendMessage")
    assert not send_message("", "222", "hello")  # missing config refuses cleanly


def test_deep_link_format():
    assert bind_deep_link("synthetic_hotel_bot", "abc123") == "https://t.me/synthetic_hotel_bot?start=abc123"


def test_generate_nonce_requires_token(db_factory):
    with db_factory.begin() as db:
        assert generate_bind_nonce(db, actor="synthetic-admin") is None


def test_bind_happy_path_start_payload_and_single_use(db_factory, monkeypatch):
    with db_factory.begin() as db:
        save_config(db, bot_token="tok", chat_id="", enabled=True, language="ar", actor="synthetic-admin")
        nonce = generate_bind_nonce(db, actor="synthetic-admin")
        expiry = get_config(db)["bind_nonce_expires_at"]
        assert expiry is not None
        assert timedelta(minutes=14) < (expiry - telegram_alerts._now()) <= timedelta(minutes=15)

        calls, fake = _fake_urlopen({"getUpdates": _updates(_private(555, f"/start {nonce}") )})
        monkeypatch.setattr(telegram_alerts.urllib.request, "urlopen", fake)
        assert bind_chat_from_nonce(db, actor="synthetic-admin") == "bound"
        config = get_config(db)
        assert config["chat_id"] == "555" and config["bind_nonce_used"]
        # Single-use: a second confirm never rebinds.
        assert bind_chat_from_nonce(db, actor="synthetic-admin") == "used"


def test_bind_plain_text_nonce(db_factory, monkeypatch):
    with db_factory.begin() as db:
        save_config(db, bot_token="tok", chat_id="", enabled=True, language="ar", actor="synthetic-admin")
        nonce = generate_bind_nonce(db, actor="synthetic-admin")
        _, fake = _fake_urlopen({"getUpdates": _updates(_private(777, nonce))})
        monkeypatch.setattr(telegram_alerts.urllib.request, "urlopen", fake)
        assert bind_chat_from_nonce(db, actor="synthetic-admin") == "bound"
        assert get_config(db)["chat_id"] == "777"


def test_bind_rejections(db_factory, monkeypatch):
    with db_factory.begin() as db:
        save_config(db, bot_token="tok", chat_id="", enabled=True, language="ar", actor="synthetic-admin")
        # No nonce issued yet.
        assert bind_chat_from_nonce(db, actor="synthetic-admin") == "no_nonce"
        nonce = generate_bind_nonce(db, actor="synthetic-admin")

        # Messages exist but none carries the nonce: no fallback to any chat.
        _, fake = _fake_urlopen({"getUpdates": _updates(_private(111, "hello"), _private(222, "/start"))})
        monkeypatch.setattr(telegram_alerts.urllib.request, "urlopen", fake)
        assert bind_chat_from_nonce(db, actor="synthetic-admin") == "no_match"
        assert get_config(db)["chat_id"] == ""

        # Nonce arriving in a group is rejected.
        _, fake = _fake_urlopen({"getUpdates": _updates(
            {"chat": {"id": -100999, "type": "group"}, "text": f"/start {nonce}"})})
        monkeypatch.setattr(telegram_alerts.urllib.request, "urlopen", fake)
        assert bind_chat_from_nonce(db, actor="synthetic-admin") == "group_rejected"
        assert get_config(db)["chat_id"] == ""

        # The same nonce from two different private chats is ambiguous: refuse.
        _, fake = _fake_urlopen({"getUpdates": _updates(
            _private(111, f"/start {nonce}"), _private(222, nonce))})
        monkeypatch.setattr(telegram_alerts.urllib.request, "urlopen", fake)
        assert bind_chat_from_nonce(db, actor="synthetic-admin") == "multiple"
        assert get_config(db)["chat_id"] == ""

        # Expired nonce never binds, even with a perfect message.
        record = telegram_alerts._load_record(db)
        record["bind_nonce_expires_at"] = (telegram_alerts._now() - timedelta(minutes=1)).isoformat()
        telegram_alerts._save_record(db, record, "synthetic-admin")
        _, fake = _fake_urlopen({"getUpdates": _updates(_private(111, f"/start {nonce}"))})
        monkeypatch.setattr(telegram_alerts.urllib.request, "urlopen", fake)
        assert bind_chat_from_nonce(db, actor="synthetic-admin") == "expired"
        assert get_config(db)["chat_id"] == ""


def test_bind_api_error_never_raises(db_factory, monkeypatch):
    def _boom(request, timeout=None):
        raise OSError("network down")

    with db_factory.begin() as db:
        save_config(db, bot_token="tok", chat_id="", enabled=True, language="ar", actor="synthetic-admin")
        generate_bind_nonce(db, actor="synthetic-admin")
        monkeypatch.setattr(telegram_alerts.urllib.request, "urlopen", _boom)
        assert bind_chat_from_nonce(db, actor="synthetic-admin") == "api_error"


def test_token_never_leaks_through_errors(monkeypatch):
    # Even when the transport error message itself embeds the request URL
    # (and therefore the token), the failure is swallowed whole: no raise,
    # no propagation, and the module has no logging calls to carry it.
    def _leaky(request, timeout=None):
        raise OSError(f"boom while opening {request.full_url}")

    monkeypatch.setattr(telegram_alerts.urllib.request, "urlopen", _leaky)
    secret = "111:SECRET-TOKEN"
    assert send_message(secret, "1", "x") is False
    assert verify_token(secret) is None


def test_no_send_when_unbound_or_disabled(db_factory, monkeypatch):
    calls, fake = _fake_urlopen({"sendMessage": {"ok": True, "result": {}}})
    monkeypatch.setattr(telegram_alerts.urllib.request, "urlopen", fake)

    import app.telegram_alerts as module
    import app.db as db_module
    monkeypatch.setattr(db_module, "SessionLocal", db_factory)

    with db_factory.begin() as db:
        # Token saved but no chat bound: nothing may leave.
        save_config(db, bot_token="tok", chat_id="", enabled=True, language="ar", actor="synthetic-admin")
    assert module.send_login_event("login_success", "admin") is False
    with db_factory.begin() as db:
        assert module.send_test_message(db) is False
    assert calls == []

    with db_factory.begin() as db:
        # Bound but disabled: nothing may leave.
        save_config(db, bot_token="tok", chat_id="777", enabled=False, language="ar", actor="synthetic-admin")
    assert module.send_login_event("login_success", "admin") is False
    assert calls == []


def test_failures_never_raise(monkeypatch):
    def _boom(request, timeout=None):
        raise OSError("network down")

    monkeypatch.setattr(telegram_alerts.urllib.request, "urlopen", _boom)
    assert verify_token("tok") is None
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
