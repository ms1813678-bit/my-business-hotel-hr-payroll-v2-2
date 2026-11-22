"""Free Telegram Bot API transport for login security alerts.

Sends lockout and successful-login alerts to the owner's Telegram chat via the
official Bot API (https://core.telegram.org/bots/api): free, ToS-compliant,
and no automation of anyone's personal account. The bot cannot initiate the
conversation, so the owner binds their private chat once with a single-use
nonce: the app issues a short-lived binding link, the owner opens it in
Telegram and taps Start, and only the chat that returns that exact nonce is
bound. There is no "latest chat" fallback: possessing the bot token never
makes an arbitrary chat the owner.

Alerting must never break sign-in: every network or configuration problem is
swallowed and reported as False.
"""
from __future__ import annotations

import json
import os
import secrets
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import SystemSetting

SETTING_KEY = "telegram_alerts"
_API_BASE = "https://api.telegram.org"
_TIMEOUT_SECONDS = 5
_BIND_TTL = timedelta(minutes=15)

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


def _save_record(db: Session, record: dict, actor: str) -> None:
    setting = db.get(SystemSetting, SETTING_KEY)
    if setting is None:
        setting = SystemSetting(key=SETTING_KEY, value="", updated_by=actor)
        db.add(setting)
    setting.value = json.dumps(record, ensure_ascii=False)
    setting.updated_by = actor
    db.flush()


def _expiry_from(record: dict) -> datetime | None:
    raw = record.get("bind_nonce_expires_at")
    if not raw:
        return None
    try:
        expiry = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry


def get_config(db: Session) -> dict:
    """Effective Telegram alert configuration (database record + env fallback)."""
    record = _load_record(db) or {}
    expiry = _expiry_from(record)
    return {
        "bot_token": (record.get("bot_token") or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip(),
        "bot_username": (record.get("bot_username") or "").strip(),
        "chat_id": (record.get("chat_id") or os.environ.get("TELEGRAM_CHAT_ID", "")).strip(),
        "enabled": bool(record.get("enabled", True)),
        "language": record.get("language") if record.get("language") in _LANGUAGES else "ar",
        "bind_nonce": record.get("bind_nonce") or "",
        "bind_nonce_used": bool(record.get("bind_nonce_used", False)),
        "bind_nonce_expires_at": expiry,
    }


def save_config(
    db: Session,
    *,
    bot_token: str,
    chat_id: str,
    enabled: bool,
    language: str,
    actor: str,
    bot_username: str | None = None,
    clear_binding: bool = False,
) -> None:
    """Persist settings, preserving binding state unless the bot itself changed."""
    if language not in _LANGUAGES:
        language = "ar"
    record = _load_record(db) or {}
    record.update({
        "bot_token": bot_token.strip(),
        "chat_id": chat_id.strip(),
        "enabled": bool(enabled),
        "language": language,
    })
    if bot_username is not None:
        record["bot_username"] = bot_username.strip()
    if clear_binding:
        record["chat_id"] = ""
        record["bind_nonce"] = ""
        record["bind_nonce_expires_at"] = ""
        record["bind_nonce_used"] = False
    _save_record(db, record, actor)


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


def generate_bind_nonce(db: Session, *, actor: str) -> str | None:
    """Issue a single-use, 15-minute binding nonce; None when no token is saved."""
    config = get_config(db)
    if not config["bot_token"]:
        return None
    record = _load_record(db) or {}
    nonce = secrets.token_urlsafe(6)
    record["bind_nonce"] = nonce
    record["bind_nonce_expires_at"] = (_now() + _BIND_TTL).isoformat()
    record["bind_nonce_used"] = False
    _save_record(db, record, actor)
    return nonce


def bind_deep_link(bot_username: str, nonce: str) -> str:
    """Telegram deep link that carries the nonce back as the /start payload."""
    return f"https://t.me/{bot_username}?start={nonce}"


def bind_chat_from_nonce(db: Session, *, actor: str) -> str:
    """Bind the one private chat that returned the live nonce.

    Returns a status string: "bound", "no_token", "no_nonce", "used",
    "expired", "api_error", "no_match", "multiple", or "group_rejected".
    Only an exact, unexpired, unused nonce from exactly one private chat binds;
    there is deliberately no fallback to the latest or any other chat.
    """
    config = get_config(db)
    if not config["bot_token"]:
        return "no_token"
    nonce = config["bind_nonce"]
    if not nonce:
        return "no_nonce"
    if config["bind_nonce_used"]:
        return "used"
    expiry = config["bind_nonce_expires_at"]
    if expiry is None or expiry <= _now():
        return "expired"
    body = _api_call(config["bot_token"], "getUpdates", {"limit": 50})
    if body is None:
        return "api_error"
    matches: dict[str, str] = {}
    accepted_texts = {nonce, f"/start {nonce}"}
    for update in body.get("result") or []:
        message = update.get("message") or {}
        text = (message.get("text") or "").strip()
        chat = message.get("chat") or {}
        if text in accepted_texts and chat.get("id") is not None:
            matches[str(chat["id"])] = str(chat.get("type") or "")
    if not matches:
        return "no_match"
    if len(matches) > 1:
        return "multiple"
    chat_id, chat_type = next(iter(matches.items()))
    if chat_type != "private":
        return "group_rejected"
    record = _load_record(db) or {}
    record["chat_id"] = chat_id
    record["bind_nonce_used"] = True
    _save_record(db, record, actor)
    return "bound"


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
