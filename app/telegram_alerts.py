"""Free Telegram Bot API transport for login security alerts.

Sends lockout and successful-login alerts to the owner's Telegram chat via the
official Bot API (https://core.telegram.org/bots/api): free, ToS-compliant,
and no automation of anyone's personal account. The bot cannot initiate the
conversation, so the owner taps START once; the chat id is then detected from
the bot's recent updates.

Alerting must never break sign-in: every network or configuration problem is
swallowed and reported as False.
"""
from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import SystemSetting

SETTING_KEY = "telegram_alerts"
_API_BASE = "https://api.telegram.org"
_TIMEOUT_SECONDS = 5

_LANGUAGES = ("ar", "en", "zh")

_MESSAGES = {
    "login_success": {
        "ar": "تنبيه دخول - {app}: تم تسجيل دخول ناجح باسم المستخدم «{username}» في {at}. إذا لم تكن أنت، غيّر كلمة المرور فورًا.",
        "en": "Login alert - {app}: successful sign-in for user \"{username}\" at {at}. If this was not you, change the password immediately.",
        "zh": "登录提醒 - {app}:用户“{username}”于 {at} 成功登录。如果不是本人操作,请立即更改密码。",
    },
    "login_lockout": {
        "ar": "تنبيه أمني - {app}: تم إيقاف الدخول مؤقتًا لمدة {minutes} دقيقة بعد {attempts} محاولات فاشلة لاسم المستخدم «{username}» في {at}. إذا لم تكن أنت، راقب النظام وغيّر كلمة المرور.",
        "en": "Security alert - {app}: sign-in locked for {minutes} minutes after {attempts} failed attempts for user \"{username}\" at {at}. If this was not you, watch the system and change the password.",
        "zh": "安全提醒 - {app}:用户“{username}”于 {at} 连续 {attempts} 次登录失败,已锁定 {minutes} 分钟。如果不是本人操作,请关注系统并更改密码。",
    },
    "test": {
        "ar": "رسالة تجريبية - {app}: تنبيهات تيليجرام تعمل بنجاح في {at}.",
        "en": "Test message - {app}: Telegram alerts are working at {at}.",
        "zh": "测试消息 - {app}:Telegram 提醒在 {at} 运行正常。",
    },
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _load_record(db: Session) -> dict | None:
    setting = db.get(SystemSetting, SETTING_KEY)
    if setting is None or not (setting.value or "").strip():
        return None
    try:
        record = json.loads(setting.value)
    except json.JSONDecodeError:
        return None
    return record if isinstance(record, dict) else None


def get_config(db: Session) -> dict:
    """Effective Telegram alert configuration (database record + env fallback)."""
    record = _load_record(db) or {}
    return {
        "bot_token": (record.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip(),
        "chat_id": (record.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")).strip(),
        "enabled": bool(record.get("enabled", True)),
        "language": record.get("language") if record.get("language") in _LANGUAGES else "ar",
    }


def save_config(
    db: Session,
    *,
    bot_token: str,
    chat_id: str,
    enabled: bool,
    language: str,
    actor: str,
) -> None:
    if language not in _LANGUAGES:
        language = "ar"
    record = {
        "bot_token": bot_token.strip(),
        "chat_id": chat_id.strip(),
        "enabled": bool(enabled),
        "language": language,
    }
    setting = db.get(SystemSetting, SETTING_KEY)
    if setting is None:
        setting = SystemSetting(key=SETTING_KEY, value="", updated_by=actor)
        db.add(setting)
    setting.value = json.dumps(record, ensure_ascii=False)
    setting.updated_by = actor
    db.flush()


def _api_call(token: str, method: str, payload: dict) -> dict | None:
    url = f"{_API_BASE}/bot{urllib.parse.quote(token, safe=':')}/{method}"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except Exception:
        return None
    return body if isinstance(body, dict) and body.get("ok") else None


def verify_token(token: str) -> str | None:
    """Return the bot's username when the token is valid, else None."""
    body = _api_call(token.strip(), "getMe", {})
    if body:
        result = body.get("result") or {}
        username = result.get("username")
        if isinstance(username, str):
            return username
    return None


def detect_chat_id(token: str) -> str | None:
    """Return the chat id of the most recent message sent to the bot, if any."""
    body = _api_call(token.strip(), "getUpdates", {"limit": 20})
    if not body:
        return None
    chat_id: str | None = None
    for update in body.get("result") or []:
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is not None:
            chat_id = str(chat["id"])
    return chat_id


def build_alert(event: str, username: str, language: str, *, failed_attempts: int | None = None,
                locked_minutes: int | None = None, app_name: str = "") -> str:
    templates = _MESSAGES.get(event, _MESSAGES["test"])
    template = templates.get(language) or templates["ar"]
    return template.format(
        app=app_name or "M.Y. Business Hotel",
        username=username,
        at=_now().astimezone().strftime("%Y-%m-%d %H:%M"),
        attempts=failed_attempts or 0,
        minutes=locked_minutes or 0,
    )


def send_message(token: str, chat_id: str, text: str) -> bool:
    """Send one alert message; returns True on Telegram's acknowledgement."""
    if not token.strip() or not chat_id.strip():
        return False
    body = _api_call(token.strip(), "sendMessage", {"chat_id": chat_id.strip(), "text": text})
    return body is not None


def send_login_event(event: str, username: str, *, failed_attempts: int | None = None,
                     locked_minutes: int | None = None, app_name: str = "") -> bool:
    """Send a login alert through Telegram when configured; never raises."""
    from app.db import SessionLocal

    try:
        with SessionLocal() as db:
            config = get_config(db)
        if not config["enabled"] or not config["bot_token"] or not config["chat_id"]:
            return False
        text = build_alert(
            event,
            username,
            config["language"],
            failed_attempts=failed_attempts,
            locked_minutes=locked_minutes,
            app_name=app_name,
        )
        return send_message(config["bot_token"], config["chat_id"], text)
    except Exception:
        return False


def send_test_message(db: Session) -> bool:
    config = get_config(db)
    if not config["bot_token"] or not config["chat_id"]:
        return False
    return send_message(
        config["bot_token"],
        config["chat_id"],
        build_alert("test", "", config["language"]),
    )
