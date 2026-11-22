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

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AuditLog, SystemSetting

FAILED_ATTEMPTS_LIMIT = 2
LOCKOUT_DURATION = timedelta(hours=2)
LOCKOUT_MINUTES = int(LOCKOUT_DURATION.total_seconds() // 60)
_SETTING_KEY = "login_security"
_WEBHOOK_TIMEOUT_SECONDS = 3


# The forgotten-password recovery flow has its own independent counter so an
# attacker engaging the login lockout cannot also block legitimate recovery,
# and recovery probing does not consume login attempts.
RECOVERY_FAILED_LIMIT = 5
RECOVERY_LOCKOUT_DURATION = timedelta(minutes=30)
RECOVERY_LOCKOUT_MINUTES = int(RECOVERY_LOCKOUT_DURATION.total_seconds() // 60)
_RECOVERY_SETTING_KEY = "recovery_security"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _read_state(db: Session, key: str = _SETTING_KEY, *, for_update: bool = False) -> dict:
    query = db.query(SystemSetting).filter(SystemSetting.key == key)
    if for_update:
        # Serialize concurrent counters on this row: without the row lock a
        # burst of parallel failures reads the same state and both loses
        # attempts and writes duplicate lockout audit rows.
        query = query.with_for_update()
    setting = query.one_or_none()
    if setting is None:
        return {"failed_attempts": 0, "locked_until": None}
    try:
        data = json.loads(setting.value)
    except (json.JSONDecodeError, TypeError):
        return {"failed_attempts": 0, "locked_until": None}
    failed = data.get("failed_attempts") or 0
    return {"failed_attempts": int(failed), "locked_until": data.get("locked_until")}


def _write_state(db: Session, state: dict, actor: str, key: str = _SETTING_KEY) -> None:
    setting = db.get(SystemSetting, key)
    if setting is None:
        setting = SystemSetting(key=key, value="", updated_by=actor)
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


def recovery_lockout_remaining(db: Session, *, now: datetime | None = None) -> timedelta | None:
    """Remaining recovery lockout, or None when recovery attempts are allowed."""
    now = now or _now()
    raw = _read_state(db, _RECOVERY_SETTING_KEY)["locked_until"]
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


def _record_failure_locked(db: Session, key: str, limit: int, duration: timedelta,
                           audit_action: str, audit_reason: str,
                           *, actor: str, now: datetime) -> bool:
    """Atomic read/modify/write of one throttle counter. The state row is
    locked FOR UPDATE (created first when missing, retrying on the unique
    race), and the lockout audit row is written only on the transition into
    the lock, never for attempts that arrive while it is already engaged."""
    for _ in range(3):
        setting = db.query(SystemSetting).filter(SystemSetting.key == key).with_for_update().one_or_none()
        if setting is not None:
            break
        try:
            with db.begin_nested():
                db.add(SystemSetting(key=key, value='{"failed_attempts": 0, "locked_until": null}', updated_by=actor))
            db.flush()
        except IntegrityError:
            continue
    state = _read_state(db, key, for_update=True)
    now_locked_until = state.get("locked_until")
    already_locked = False
    if now_locked_until:
        try:
            parsed = datetime.fromisoformat(now_locked_until)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            already_locked = parsed > now
        except (ValueError, TypeError):
            already_locked = False
    state["failed_attempts"] += 1
    engaged = False
    if state["failed_attempts"] >= limit and not already_locked:
        state["locked_until"] = (now + duration).isoformat()
        engaged = True
        db.add(AuditLog(
            actor=actor,
            action=audit_action,
            entity_type="admin_account",
            entity_id="admin",
            reason=audit_reason.format(count=state["failed_attempts"]),
        ))
    _write_state(db, state, actor, key)
    db.commit()
    return engaged


def record_failed_recovery(db: Session, *, actor: str = "system", now: datetime | None = None) -> bool:
    """Count one failed recovery attempt. Returns True when the lockout engages."""
    now = now or _now()
    return _record_failure_locked(
        db, _RECOVERY_SETTING_KEY, RECOVERY_FAILED_LIMIT, RECOVERY_LOCKOUT_DURATION,
        "recovery_lockout", "{count} failed recovery attempts", actor=actor, now=now,
    )


# Failed reauthentication on sensitive in-session actions (account reset,
# recovery-code generation, Telegram alert settings) gets its own throttle so
# a hijacked unlocked session cannot guess the administrator's password
# without limit.
REAUTH_FAILED_LIMIT = 5
REAUTH_LOCKOUT_DURATION = timedelta(minutes=30)
REAUTH_LOCKOUT_MINUTES = int(REAUTH_LOCKOUT_DURATION.total_seconds() // 60)
_REAUTH_SETTING_KEY = "reauth_security"


def reauth_lockout_remaining(db: Session, *, now: datetime | None = None) -> timedelta | None:
    """Remaining reauth lockout, or None when reauthentication is allowed."""
    now = now or _now()
    raw = _read_state(db, _REAUTH_SETTING_KEY)["locked_until"]
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


def record_failed_reauth(db: Session, *, actor: str = "system", now: datetime | None = None) -> bool:
    """Count one failed reauthentication. Returns True when the lockout engages."""
    now = now or _now()
    return _record_failure_locked(
        db, _REAUTH_SETTING_KEY, REAUTH_FAILED_LIMIT, REAUTH_LOCKOUT_DURATION,
        "reauth_lockout", "{count} failed reauthentication attempts", actor=actor, now=now,
    )


def clear_reauth_attempts(db: Session, *, actor: str = "system") -> None:
    """Reset the reauthentication failure counter after a successful proof."""
    state = _read_state(db, _REAUTH_SETTING_KEY)
    if state["failed_attempts"] or state["locked_until"]:
        _write_state(db, {"failed_attempts": 0, "locked_until": None}, actor, _REAUTH_SETTING_KEY)
        db.commit()


def clear_recovery_attempts(db: Session, *, actor: str = "system") -> None:
    """Reset the recovery failure counter after a successful recovery."""
    state = _read_state(db, _RECOVERY_SETTING_KEY)
    if state["failed_attempts"] or state["locked_until"]:
        _write_state(db, {"failed_attempts": 0, "locked_until": None}, actor, _RECOVERY_SETTING_KEY)
        db.commit()
