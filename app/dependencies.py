"""FastAPI dependencies for validating the administrator session."""

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from datetime import datetime, timezone

from app.db import get_db
from app.models import AdminAccount, AdminSession, SystemSetting

ROLE_SYSTEM_ADMIN = "system_admin"
ROLE_FINANCE = "finance"
ROLE_ASSISTANT = "assistant_manager"

ROLE_LABELS = {
    ROLE_SYSTEM_ADMIN: "مدير النظام",
    ROLE_FINANCE: "مدير المالية",
    ROLE_ASSISTANT: "مساعد المدير",
}


def get_current_admin(
    request: Request,
    db: Session = Depends(get_db),
) -> AdminAccount:
    """Resolve the session username to an existing, active account."""
    username = request.session.get("admin_username")
    if not isinstance(username, str) or not username.strip():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="يجب تسجيل الدخول أولاً.",
            headers={"WWW-Authenticate": "Session"},
        )

    admin = (
        db.query(AdminAccount)
        .filter(AdminAccount.username == username.strip())
        .one_or_none()
    )
    session_version = request.session.get("admin_session_version")
    if (
        admin is None
        or not admin.active
        or not isinstance(session_version, int)
        or session_version != admin.session_version
    ):
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="الجلسة غير صالحة. يرجى تسجيل الدخول مرة أخرى.",
            headers={"WWW-Authenticate": "Session"},
        )

    # The signed cookie must reference a live server-side session record;
    # deleting that record (logout, manual termination, account revocation)
    # ends the session even if the cookie itself is still validly signed.
    session_id = request.session.get("admin_session_id")
    session_row = (
        db.get(AdminSession, session_id)
        if isinstance(session_id, str) and session_id
        else None
    )
    if session_row is not None and session_row.admin_account_id == admin.id:
        now = datetime.now(timezone.utc)
        last_seen = session_row.last_seen_at
        if last_seen is not None and last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        if last_seen is None or (now - last_seen).total_seconds() > 60:
            session_row.last_seen_at = now
            db.commit()
    else:
        session_row = None
    if session_row is None:
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="الجلسة غير صالحة. يرجى تسجيل الدخول مرة أخرى.",
            headers={"WWW-Authenticate": "Session"},
        )

    # An account holding a one-time reset password may only open the
    # password page (and logout) until it sets a personal password.
    if getattr(admin, "must_change_password", False):
        path = request.url.path
        allowed = path in ("/settings/password", "/logout") or path.startswith(("/static/", "/i18n/"))
        if not allowed:
            if "text/html" in request.headers.get("accept", ""):
                raise HTTPException(
                    status_code=status.HTTP_303_SEE_OTHER,
                    detail="forced-password-change",
                    headers={"Location": "/settings/password?forced=1"},
                )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="يجب تغيير كلمة المرور المؤقتة قبل المتابعة.",
            )

    # After a backup restore, the system administrator must regenerate
    # recovery codes (invalidating any resurrected ones) before anything else.
    if admin.role == ROLE_SYSTEM_ADMIN:
        regen = db.get(SystemSetting, "post_restore_regen_required")
        if regen is not None and regen.value:
            path = request.url.path
            allowed = (
                path in ("/settings/accounts", "/settings/accounts/recovery-codes",
                          "/settings/password", "/logout")
                or path.startswith(("/static/", "/i18n/"))
            )
            if not allowed:
                if "text/html" in request.headers.get("accept", ""):
                    raise HTTPException(
                        status_code=status.HTTP_303_SEE_OTHER,
                        detail="post-restore-reconciliation",
                        headers={"Location": "/settings/accounts?error=regen_required"},
                    )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="أعد إنشاء رموز الاستعادة أولًا بعد استرجاع النسخة الاحتياطية.",
                )

    # Expose identity to templates without threading it through every context.
    request.state.admin_role = admin.role
    request.state.admin_name = admin.display_name or admin.username
    return admin


def require_roles(*roles: str):
    """Allow only the named roles; every other account gets a friendly 403."""

    def dependency(admin: AdminAccount = Depends(get_current_admin)) -> AdminAccount:
        if admin.role not in roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="هذا الجزء غير متاح لصلاحية حسابك.",
            )
        return admin

    return dependency


# Role bundles matching the owner's approved account split.
require_system_admin = require_roles(ROLE_SYSTEM_ADMIN)
require_payroll_access = require_roles(ROLE_SYSTEM_ADMIN, ROLE_FINANCE)
require_attendance_write = require_roles(ROLE_SYSTEM_ADMIN, ROLE_ASSISTANT)
require_leave_read = require_roles(ROLE_SYSTEM_ADMIN, ROLE_FINANCE, ROLE_ASSISTANT)
require_attendance_read = require_roles(ROLE_SYSTEM_ADMIN, ROLE_FINANCE, ROLE_ASSISTANT)
require_employee_read = require_roles(ROLE_SYSTEM_ADMIN, ROLE_FINANCE)

# A descriptive alias kept for existing route declarations.
require_admin = get_current_admin
