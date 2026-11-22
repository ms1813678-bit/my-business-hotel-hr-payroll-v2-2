"""Brute-force protection for the single-admin login and login event alerts.

Failed attempts are counted in the ``system_settings`` table so the lockout
survives application restarts. After ``FAILED_ATTEMPTS_LIMIT`` consecutive
failures the login is locked for ``LOCKOUT_DURATION``. When a webhook URL is
configured (``LOGIN_ALERT_WEBHOOK_URL``), lockout and successful-login events
are POSTed to it as JSON so an external service can forward them (for example
to the owner's WhatsApp); sending is best-effort and never blocks or breaks
authentication.
"""
from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.config import settings
from app.models import AuditLog, SystemSetting

FAILED_ATTEMPTS_LIMIT = 2
LOCKOUT_DURATION = timedelta(hours=2)
LOCKOUT_MINUTES = int(LOCKOUT_DURATION.total_seconds() // 60)
_SETTING_KEY = "login_security"
_WEBHOOK_TIMEOUT_SECONDS = 3


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_state(db: Session) -> dict:
    setting = db.get(SystemSetting, _SETTING_KEY)
    if setting is None:
        return {"failed_attempts": 0, "locked_until": None}
    try:
        data = json.loads(setting.value)
    except (json.JSONDecodeError, TypeError):
        return {"failed_attempts": 0, "locked_until": None}
    failed = data.get("failed_attempts") or 0
    return {"failed_attempts": int(failed), "locked_until": data.get("locked_until")}


def _write_state(db: Session, state: dict, actor: str) -> None:
    setting = db.get(SystemSetting, _SETTING_KEY)
    if setting is None:
        setting = SystemSetting(key=_SETTING_KEY, value="", updated_by=actor)
        db.add(setting)
    setting.value = json.dumps(state)
    setting.updated_by = actor


def lockout_remaining(db: Session, *, now: datetime | None = None) -> timedelta | None:
    """Return the remaining lockout time, or None when login attempts are allowed."""
    now = now or _now()
    raw = _read_state(db)["locked_until"]
    if not raw:
        return None
    try:
        locked_until = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)
    remaining = locked_until - now
    return remaining if remaining.total_seconds() > 0 else None


def record_failed_attempt(db: Session, *, actor: str = "system", now: datetime | None = None) -> bool:
    """Count one failed login. Returns True exactly when this attempt engages the lockout."""
    now = now or _now()
    state = _read_state(db)
    state["failed_attempts"] += 1
    engaged = False
    if state["failed_attempts"] >= FAILED_ATTEMPTS_LIMIT:
        state["locked_until"] = (now + LOCKOUT_DURATION).isoformat()
        engaged = True
        db.add(AuditLog(
            actor=actor,
            action="login_lockout",
            entity_type="admin_account",
            entity_id="admin",
            reason=f"{state['failed_attempts']} failed login attempts",
        ))
    _write_state(db, state, actor)
    db.commit()
    return engaged


def clear_attempts(db: Session, *, actor: str = "system") -> None:
    """Reset the failure counter after a successful login."""
    state = _read_state(db)
    if state["failed_attempts"] or state["locked_until"]:
        _write_state(db, {"failed_attempts": 0, "locked_until": None}, actor)
        db.commit()


def send_login_alert(event: str, username: str, *, failed_attempts: int | None = None) -> bool:
    """POST a login event to the configured alert webhook.

    Returns True when the endpoint acknowledged the alert. Any network or
    configuration problem is swallowed: alerting must never break sign-in.
    """
    url = (getattr(settings, "login_alert_webhook_url", "") or "").strip()
    if not url:
        return False
    payload = {
        "event": event,
        "username": username,
        "at": _now().isoformat(),
        "app": settings.app_name,
    }
    if event == "login_lockout":
        payload["failed_attempts"] = failed_attempts
        payload["locked_minutes"] = LOCKOUT_MINUTES
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_WEBHOOK_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300
    except Exception:
        return False
