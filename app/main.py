import asyncio
from calendar import monthrange
import functools
import hashlib
import hmac
import math
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import uuid4
import re
import secrets
import time as time_module

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, delete, func, or_, select, text, update
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.middleware.sessions import SessionMiddleware

from app.auth import authenticate, change_admin_password
from app.config import settings
from app.csrf import csrf_protect, get_csrf_token
from app.db import SessionLocal, engine, get_db
from app.dependencies import (
    ROLE_ASSISTANT,
    ROLE_FINANCE,
    ROLE_LABELS,
    ROLE_SYSTEM_ADMIN,
    get_current_admin,
    require_attendance_read,
    require_attendance_write,
    require_leave_read,
    require_employee_read,
    require_payroll_access,
    require_system_admin,
)
from app.login_security import (
    FAILED_ATTEMPTS_LIMIT,
    LOCKOUT_MINUTES,
    RECOVERY_LOCKOUT_MINUTES,
    clear_attempts,
    clear_recovery_attempts,
    record_failed_recovery,
    reauth_lockout_remaining,
    record_failed_reauth,
    clear_reauth_attempts,
    recovery_lockout_remaining,
    lockout_remaining,
    record_failed_attempt,
    send_login_alert,
)
from app.restore_lock import acquire_credential_write_lock
from app.telegram_alerts import (
    bind_chat_from_nonce as bind_telegram_chat_from_nonce,
    bind_deep_link as telegram_bind_deep_link,
    generate_bind_nonce as generate_telegram_bind_nonce,
    get_config as telegram_get_config,
    save_config as telegram_save_config,
    send_login_event as send_telegram_login_event,
    send_test_message as send_telegram_test_message,
    verify_token as verify_telegram_token,
)
from app.security import _MIN_PASSWORD_LENGTH, hash_password, verify_password
from app.employee_codes import allocate_employee_code
from app.leave_workflow import add_balance_entry, cancel_leave, confirm_leave, leave_balance
from app.advance_workflow import approve_advance, create_advance, mark_advance_paid, reject_advance
from app.models import AdvanceInstallment, CAIRO_TZ
from app.national_id import ERROR_MESSAGE as NATIONAL_ID_ERROR, is_valid_egyptian_national_id
from app.staff_actions_workflow import (
    CATEGORY_LABELS_AR,
    approve_staff_action,
    cancel_staff_action,
    create_staff_action,
    record_employee_acknowledgement,
)
from app.reports import build_hr_pdf, build_hr_workbook
from app.report_builder import (
    ATTENDANCE_FIELD_DEFS,
    DEFAULT_FIELDS,
    EMPLOYEE_FIELD_DEFS,
    FIELD_GROUP_DEFS,
    FIELD_GROUPS_BY_TYPE,
    PAYROLL_BREAKDOWN_DEFS,
    PAYROLL_FIELD_DEFS,
    REPORT_TYPE_ADVANCES,
    REPORT_TYPE_ATTENDANCE,
    REPORT_TYPE_COMPREHENSIVE,
    REPORT_TYPE_LABELS,
    REPORT_TYPE_LEAVE,
    REPORT_TYPE_PAYROLL,
    REPORT_TYPE_STAFF_ACTIONS,
    REPORT_TYPES,
    ReportSelection,
    _parse_date,
    MONTHLY_OPTION_LABELS,
    MONTHLY_OPTION_TYPE,
    MONTHLY_ALL_OPTIONS,
    apply_config,
    build_custom_pdf,
    build_custom_workbook,
    build_monthly_pdf,
    build_report,
    monthly_options_for_types,
    list_presets,
    load_preset,
    month_label,
    preset_config,
    preset_config_from_params,
    resolve_period,
    save_preset,
    selection_from_params,
)
from app.backup_crypto import (
    PassphraseRequiredError,
    WrongPassphraseError,
    has_passphrase as backup_has_passphrase,
    set_passphrase as set_backup_passphrase,
)
from app.backup_workflow import create_backup, export_key_file, get_backup_root, restore_backup, save_backup_root
from app.payroll_workflow import (
    AbsenceLeaveConflictError,
    absence_leave_conflicts,
    add_payroll_payment,
    correct_payroll_payment,
    approve_payroll_run,
    close_payroll_run,
    create_payroll_run,
    approve_overtime,
    payment_totals,
    reopen_payroll_run,
    review_absence_leave_conflict,
    update_payroll_item,
)
from app.models import AdvanceRequest, BackupRun, SystemSetting, StaffAction, PayrollRun, PayrollItem, PayrollPayment, AdminAccount, AttendanceDailyRecord, AttendanceImportBatch, AttendanceImportIssue, AttendancePunch, AttendanceMonth, AuditLog, Branch, RecoveryCode, AdminSession, Department, Employee, EmployeeChange, EmployeeDataChangeRequest, EmployeeServicePeriod, LeaveBalanceTransaction, LeaveRecord, LeaveType, SecurityShiftAssignment, ShiftPattern
from app.security_roster_import import parse_security_roster_xlsx
from app.attendance_import import parse_attendance_workbook, normalize as normalize_attendance_alias
from app.attendance_workflow import persist_attendance_import, prepare_and_persist_monthly_reconciliation, approve_attendance_day, approve_attendance_month, approve_all_matching_attendance, bulk_approval_scope, BulkScopeMismatch, suggested_clock_pair, add_manual_attendance, resolve_unknown_attendance_employee, summarize_attendance_month_coverage
from app.security_roster_workflow import prepare_security_roster, confirm_security_roster_day
from app.ai_assistant import ask_local_model, local_fallback_answer, local_model_status
from app.ai_agent import is_command_request
from app.ai_smart_scan import build_smart_scan
from app.ai_hr_queries import is_structured_hr_query, answer_structured_hr_query
from app.ai_gateway import prepare_command, approve_command, reject_command, recent_memory, record_exchange

SECURITY_ROSTER_MAX_BYTES = 5 * 1024 * 1024
SECURITY_ROSTER_STAGING_DIR = Path("var") / "security_roster_staging"
SECURITY_ROSTER_STAGING_TTL_SECONDS = 60 * 60
SECURITY_ROSTER_CLEANUP_INTERVAL_SECONDS = 5 * 60
REPORTS_DIR = Path("var") / "reports"


def _save_report_copy(content: bytes, extension: str, start_date: date, end_date: date, employee_id: int | None = None, kind: str = "custom") -> Path:
    """Save a user-requested report locally without replacing an earlier export.
    kind is real save-time metadata (custom builder vs monthly ready report)
    recorded in the filename so the library's latest-files list can label it."""
    if extension not in {"xlsx", "pdf"} or not isinstance(content, bytes) or not content or kind not in {"custom", "monthly"}:
        raise ValueError("invalid_report_export")
    destination = None
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            REPORTS_DIR.chmod(0o700)
        except OSError:
            pass
        employee_part = f"employee-{employee_id}" if employee_id is not None else "all-employees"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        filename = f"MY_HR_Report_{start_date.isoformat()}_{end_date.isoformat()}_{employee_part}_{stamp}_{kind}_{uuid4().hex[:8]}.{extension}"
        destination = REPORTS_DIR / filename
        with destination.open("xb") as report_file:
            report_file.write(content)
        try:
            destination.chmod(0o600)
        except OSError:
            pass
        return destination
    except OSError as exc:
        if destination is not None:
            try:
                destination.unlink(missing_ok=True)
            except OSError:
                pass
        raise HTTPException(status_code=503, detail="تعذر حفظ نسخة التقرير محليًا. تحقق من صلاحيات ومساحة مجلد التقارير.") from exc


def _cleanup_expired_security_roster_files(now: float | None = None) -> int:
    """Delete expired staged workbooks independently of upload/preview actions."""
    if not SECURITY_ROSTER_STAGING_DIR.exists():
        return 0
    current_time = time_module.time() if now is None else now
    removed = 0
    for candidate in SECURITY_ROSTER_STAGING_DIR.glob("*.xlsx"):
        try:
            if current_time - candidate.stat().st_mtime > SECURITY_ROSTER_STAGING_TTL_SECONDS:
                candidate.unlink(missing_ok=True)
                removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            # A transient filesystem problem must not stop the cleanup loop.
            continue
    return removed


async def _security_roster_cleanup_loop() -> None:
    while True:
        _cleanup_expired_security_roster_files()
        await asyncio.sleep(SECURITY_ROSTER_CLEANUP_INTERVAL_SECONDS)


@asynccontextmanager
async def _app_lifespan(application):
    cleanup_task = asyncio.create_task(_security_roster_cleanup_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass


EMPLOYEE_PHOTO_DIR = Path("var") / "employee_photos"
EMPLOYEE_PHOTO_MAX_BYTES = 5 * 1024 * 1024
EMPLOYEE_PHOTO_FIELDS = {"full_name", "phone", "address", "position", "national_id"}


def _store_employee_photo(upload: UploadFile | None) -> str | None:
    if upload is None or not upload.filename:
        return None
    raw = upload.file.read(EMPLOYEE_PHOTO_MAX_BYTES + 1)
    if not raw:
        return None
    if len(raw) > EMPLOYEE_PHOTO_MAX_BYTES:
        raise HTTPException(status_code=413, detail="حجم الصورة يجب ألا يتجاوز 5 ميجابايت.")
    try:
        import io
        with Image.open(io.BytesIO(raw)) as image_file:
            image_file.verify()
            image_format = image_file.format
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise HTTPException(status_code=400, detail="ملف الصورة غير صالح.")
    extensions = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}
    extension = extensions.get(image_format)
    if extension is None:
        raise HTTPException(status_code=400, detail="الصورة يجب أن تكون JPG أو PNG أو WEBP.")
    # Phone cameras store orientation in EXIF instead of rotating pixels;
    # apply it so portraits never appear sideways or upside down.
    with Image.open(io.BytesIO(raw)) as image_file:
        oriented = ImageOps.exif_transpose(image_file)
        buffer = io.BytesIO()
        oriented.save(buffer, format=image_format)
        raw = buffer.getvalue()
    EMPLOYEE_PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid4().hex}{extension}"
    (EMPLOYEE_PHOTO_DIR / filename).write_bytes(raw)
    return filename


app = FastAPI(
    title=settings.app_name,
    docs_url="/docs" if settings.app_expose_docs else None,
    openapi_url="/openapi.json" if settings.app_expose_docs else None,
    redoc_url=None,
    lifespan=_app_lifespan,
)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    session_cookie="session",
    max_age=8 * 60 * 60,
    path="/",
    same_site="lax",
    https_only=settings.app_env.lower() == "production",
)

# Outermost middleware: every HTTP request holds the shared restore gate until
# its response finishes, so no request body can read the half-restored window
# (exempt: the restore route, credential-write routes, static/language paths).
from app.restore_lock import RestoreReadGateMiddleware

app.add_middleware(RestoreReadGateMiddleware)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

from app.report_builder import _fmt_number as _report_fmt_number

templates.env.filters["fmtnum"] = _report_fmt_number

from app.models import CAIRO_TZ


def _fmt_dt(value):
    """Display datetimes in Africa/Cairo wall time; naive values are already Cairo wall time."""
    if value is None:
        return "—"
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(CAIRO_TZ)
        return value.strftime("%d/%m/%Y %H:%M")
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    return str(value)


def _fmt_d(value):
    if value is None:
        return "—"
    if isinstance(value, (datetime, date)):
        return value.strftime("%d/%m/%Y")
    return str(value)


def _fmt_m(value):
    if value is None:
        return "—"
    if isinstance(value, (datetime, date)):
        return value.strftime("%m/%Y")
    return str(value)


# System enum values rendered as Arabic labels; the translate_html middleware
# then localizes the Arabic label into EN/ZH from the catalogs.


def _status_label(value):
    return _STATUS_AR.get(value, value)


# Employee field keys rendered as Arabic labels; translate_html localizes them.
_FIELD_LABELS = {
    "full_name": "الاسم الكامل",
    "national_id": "الرقم القومي",
    "phone": "الهاتف",
    "address": "العنوان",
    "position": "الوظيفة",
}


def _field_label(value):
    return _FIELD_LABELS.get(value, value)


# Stored attendance review_notes are "; "-joined system tokens (issue codes
# and fixed system sentences) plus free user text. Display maps each known
# token to an authored Arabic label that _translate_text localizes; unknown
# tokens and user text pass through byte-identical. Stored values never change.
_ATTENDANCE_NOTES_AR = {
    "security_roster_shift_pattern_missing": "لا يوجد نمط وردية مطابق في جدول الأمن",
    "conflicting_first_punch_shift_proposals": "أول بصمة تطابق أكثر من وردية؛ راجع الوردية المقصودة",
    "multiple_program_clock_in_events": "أحداث حضور متعددة في جدول البرنامج",
    "multiple_program_clock_out_events": "أحداث انصراف متعددة في جدول البرنامج",
    "duplicate_program_events": "أحداث مكررة في جدول البرنامج",
    "duplicate_fingerprint_events": "بصمات مكررة في هذا اليوم",
    "only_one_fingerprint_event": "بصمة واحدة فقط في هذا اليوم",
    "missing_fingerprint_evidence": "لا توجد بصمة مثبتة لهذا اليوم",
    "program_fingerprint_clock_in_mismatch": "فرق بين وقت الحضور في البرنامج والبصمة",
    "program_fingerprint_clock_out_mismatch": "فرق بين وقت الانصراف في البرنامج والبصمة",
    "clock_out_not_after_clock_in": "وقت الانصراف ليس بعد وقت الحضور",
    "incomplete_attendance_endpoints": "أوقات الحضور أو الانصراف غير مكتملة",
    "dst_wall_clock_duration_uncertain": "المدة غير مؤكدة بسبب ساعة تغيير التوقيت الصيفي",
    "Manual HR attendance entered and awaiting approval.": "إدخال حضور يدوي من الموارد البشرية في انتظار الاعتماد.",
}


def _attendance_notes_display(value, language):
    if not value:
        return value
    parts = []
    for part in str(value).split("; "):
        if part in _ATTENDANCE_NOTES_AR:
            parts.append(_translate_text(_ATTENDANCE_NOTES_AR[part], language))
        elif part.startswith("HR decision: "):
            parts.append(_translate_text("قرار الموارد البشرية", language) + ": " + part[len("HR decision: "):])
        else:
            parts.append(part)
    return "; ".join(parts)


templates.env.filters["fmt_dt"] = _fmt_dt
templates.env.filters["fmt_d"] = _fmt_d
templates.env.filters["fmt_m"] = _fmt_m
templates.env.filters["status_label"] = _status_label


# Link lines emitted by the structured answer formatters look like
# "label: /path". A rendered link must be a same-origin in-app GET page:
# exactly one leading slash, plain path segments only (no backslashes, no
# percent-encoding, no dots, no second slash that would make it a
# protocol-relative //host URL), and it must resolve to a real GET route.
_ANSWER_LINK_LINE = re.compile(
    r"^([^:\n]{1,80}):\s*(/[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-]+)*)/?$"
)


def _get_route_paths():
    return {
        route.path
        for route in app.routes
        if getattr(route, "methods", None) and "GET" in route.methods
    }


def _answer_links(text):
    """Extract verified in-app record links from assistant answer lines."""
    routes = _get_route_paths()
    links = []
    for line in (text or "").splitlines():
        match = _ANSWER_LINK_LINE.match(line.strip())
        if match and match.group(2) in routes:
            links.append((match.group(1).strip(), match.group(2)))
    return links


def _answer_text_without_links(text):
    """The same answer text with link lines removed (they render as links)."""
    routes = _get_route_paths()
    kept = []
    for line in (text or "").splitlines():
        match = _ANSWER_LINK_LINE.match(line.strip())
        if match and match.group(2) in routes:
            continue
        kept.append(line)
    return "\n".join(kept)


templates.env.filters["answer_links"] = _answer_links
templates.env.filters["answer_text_without_links"] = _answer_text_without_links
templates.env.filters["field_label"] = _field_label

from app.audit_display import detail_label as _audit_detail_label, reason_label as _audit_reason_label, system_reason as _system_reason_label, STATUS_LABELS_AR as _STATUS_AR


def _leave_type_name(item, language: str) -> str:
    """Per-locale leave type name: AR/EN come from stored data; ZH translates
    known Arabic names and otherwise falls back to the stored English name,
    so Arabic never leaks into the EN/ZH UI."""
    if language == "ar":
        return item.name_ar
    if language == "en":
        return item.name_en or item.name_ar
    stored_zh = getattr(item, "name_zh", None)
    if stored_zh and stored_zh.strip():
        return stored_zh.strip()
    translated = _translate_text(item.name_ar, "zh")
    if translated != item.name_ar:
        return translated
    return item.name_en or item.name_ar


templates.env.globals["audit_detail"] = _audit_detail_label
templates.env.globals["audit_reason"] = _audit_reason_label
templates.env.globals["system_reason"] = _system_reason_label
templates.env.globals["leave_type_name"] = _leave_type_name
templates.env.globals["get_csrf_token"] = get_csrf_token

from app.i18n import translate_html, translate_text as _translate_text

SUPPORTED_LANGUAGES = {"ar", "en", "zh"}

@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.middleware("http")
async def language_middleware(request: Request, call_next):
    language = request.cookies.get("my_hr_language", "ar")
    if language not in SUPPORTED_LANGUAGES:
        language = "ar"
    request.state.language = language
    response = await call_next(request)
    content_type = response.headers.get("content-type", "")
    if content_type.lower().startswith("text/html"):
        if hasattr(response, "body"):
            body = response.body
        elif hasattr(response, "body_iterator"):
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
            body = b"".join(chunks)
        else:
            body = b""
        body = translate_html(body.decode("utf-8"), language).encode("utf-8")
        headers = dict(response.headers)
        headers.pop("content-length", None)
        headers["content-language"] = {"ar": "ar", "en": "en", "zh": "zh-CN"}[language]
        response = Response(
            content=body,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
            background=response.background,
        )
    return response


@app.get("/language/{language}")
def set_language(language: str, request: Request):
    if language not in SUPPORTED_LANGUAGES:
        raise HTTPException(status_code=404, detail="Unsupported language")
    referer = request.headers.get("referer", "/")
    parsed = urlsplit(referer)
    if parsed.scheme in {"http", "https"} and parsed.hostname == request.url.hostname:
        destination = parsed.path or "/"
        if parsed.query:
            destination += "?" + parsed.query
    elif referer.startswith("/") and not referer.startswith("//"):
        destination = referer
    else:
        destination = "/"
    response = RedirectResponse(destination, status_code=303)
    response.set_cookie(
        "my_hr_language", language, max_age=60 * 60 * 24 * 365,
        httponly=True, secure=settings.app_env.lower() == "production",
        samesite="lax", path="/",
    )
    return response



def _wants_html(request: Request) -> bool:
    accept = request.headers.get("accept", "").lower()
    return "text/html" in accept and "application/json" not in accept


def _friendly_error_page(request: Request, status_code: int, heading: str, body: str) -> HTMLResponse:
    """Standalone trilingual error page. Strings are Arabic source, translated here."""
    import html as _html
    from app.i18n import translate_text
    language = getattr(request.state, "language", "ar")
    heading = _html.escape(translate_text(heading, language))
    body = _html.escape(translate_text(body, language))
    home = _html.escape(translate_text("العودة للرئيسية", language))
    html = (
        "<!DOCTYPE html><html lang=\"ar\" dir=\"rtl\"><head>"
        "<meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{status_code} — {heading}</title>"
        "<style>body{font-family:sans-serif;background:#f4f6f8;color:#1f2933;"
        "display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;}"
        ".card{background:#fff;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.08);"
        "padding:32px;max-width:420px;text-align:center;}"
        ".code{font-size:48px;font-weight:700;color:#0d6efd;margin:0;}"
        "h1{font-size:20px;margin:8px 0;}"
        "p{color:#52606d;line-height:1.7;}"
        "a{display:inline-block;margin-top:16px;background:#0d6efd;color:#fff;"
        "padding:10px 20px;border-radius:8px;text-decoration:none;}</style></head><body>"
        f"<div class=\"card\"><p class=\"code\">{status_code}</p>"
        f"<h1>{heading}</h1><p>{body}</p>"
        "<a href=\"/\">" + home + "</a></div></body></html>"
    )
    return HTMLResponse(status_code=status_code, content=html)


from starlette.exceptions import HTTPException as StarletteHTTPException


@app.exception_handler(StarletteHTTPException)
@app.exception_handler(HTTPException)
async def authentication_exception_handler(request: Request, exc: HTTPException):
    """Redirect browser page requests to login while preserving API error responses."""
    wants_html = _wants_html(request)
    if exc.status_code == 401 and request.method in {"GET", "HEAD"} and wants_html:
        return RedirectResponse("/login", status_code=303)
    if wants_html and exc.status_code in {403, 404}:
        # App-authored Arabic details are user-facing reasons; keep them.
        # Anything else (framework "Not Found", programmer strings) gets a
        # neutral fallback so no internals leak into the page.
        detail = exc.detail if isinstance(exc.detail, str) else ""
        has_arabic_reason = any("\u0600" <= ch <= "\u06ff" for ch in detail)
        if exc.status_code == 403:
            heading = "غير مسموح بهذا الإجراء"
            fallback = "ليس لديك صلاحية تنفيذ هذا الإجراء. ارجع للرئيسية."
        else:
            heading = "الصفحة غير موجودة"
            fallback = "الرابط غير صحيح أو الصفحة اتنقلت. ارجع للرئيسية وكمّل من هناك."
        return _friendly_error_page(
            request, exc.status_code, heading, detail if has_arabic_reason else fallback,
        )
    from fastapi.responses import JSONResponse
    detail = exc.detail
    if isinstance(detail, str):
        from app.i18n import translate_text
        detail = translate_text(detail, getattr(request.state, "language", "ar"))
    return JSONResponse(status_code=exc.status_code, content={"detail": detail}, headers=exc.headers)


from fastapi.exceptions import RequestValidationError


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Localize 422 payloads; keep type/loc/msg structure, never echo inputs."""
    from fastapi.responses import JSONResponse
    from app.i18n import translate_text
    from app.validation_labels import FIELD_LABELS_AR, MSG_INVALID_AR, MSG_REQUIRED_AR

    language = getattr(request.state, "language", "ar")
    detail = []
    for error in exc.errors():
        loc = [str(part) for part in error.get("loc", [])]
        field_key = loc[-1] if loc else ""
        label_ar = FIELD_LABELS_AR.get(field_key, field_key)
        msg_ar = MSG_REQUIRED_AR if error.get("type") == "missing" else MSG_INVALID_AR
        detail.append({
            "type": error.get("type"),
            "loc": loc,
            "field": translate_text(label_ar, language),
            "msg": translate_text(msg_ar, language),
        })
    if _wants_html(request):
        problems = ". ".join(f"{d['field']}: {d['msg']}" for d in detail[:3])
        retry = translate_text("ارجع وجرّب مرة أخرى.", language)
        return _friendly_error_page(
            request, 422, "البيانات غير مكتملة أو غير صحيحة",
            f"{problems} {retry}",
        )
    return JSONResponse(status_code=422, content={"detail": detail})


@app.get("/health")
def health():
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected"}
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail={"status": "degraded", "database": "unavailable"})


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    context = {
        "error": None,
        "recovered": request.query_params.get("recovered") == "1",
        "csrf_token": get_csrf_token(request),
    }
    with SessionLocal() as db:
        remaining = lockout_remaining(db)
        context["backup_passphrase_required"] = _post_restore_passphrase_armed(db)
    if remaining is not None:
        context["error"] = "تم إيقاف الدخول مؤقتًا بعد كثرة المحاولات غير الصحيحة. حاول مرة أخرى بعد"
        context["lockout_minutes"] = max(1, math.ceil(remaining.total_seconds() / 60))
        context["lockout_unit"] = "دقيقة."
    return templates.TemplateResponse(
        request=request, name="login.html",
        context=context,
    )


def _post_restore_passphrase_armed(db) -> bool:
    flag = db.get(SystemSetting, "post_restore_passphrase_required")
    return bool(flag is not None and flag.value)


@app.post("/login", dependencies=[Depends(csrf_protect)])
def login(request: Request, username: str = Form(...), password: str = Form(...),
          backup_passphrase: str = Form("")):
    with SessionLocal() as db:
        # Exclude restores: credentials are verified and sessions written only
        # before a restore snapshot or after its finalization, never mid-swap.
        acquire_credential_write_lock(db)
        remaining = lockout_remaining(db)
        if remaining is not None:
            minutes = max(1, math.ceil(remaining.total_seconds() / 60))
            return templates.TemplateResponse(
                request=request, name="login.html",
                context={
                    "error": "تم إيقاف الدخول مؤقتًا بعد كثرة المحاولات غير الصحيحة. حاول مرة أخرى بعد",
                    "lockout_minutes": minutes,
                    "lockout_unit": "دقيقة.",
                    "csrf_token": get_csrf_token(request),
                },
                status_code=429,
            )
        admin = authenticate(db, username.strip(), password)
        if admin is None:
            engaged = record_failed_attempt(db, actor="login")
            if engaged:
                send_login_alert("login_lockout", username.strip(), failed_attempts=FAILED_ATTEMPTS_LIMIT)
                send_telegram_login_event(
                    "login_lockout", username.strip(),
                    failed_attempts=FAILED_ATTEMPTS_LIMIT, locked_minutes=LOCKOUT_MINUTES,
                    app_name=settings.app_name,
                )
                return templates.TemplateResponse(
                    request=request, name="login.html",
                    context={
                        "error": "تم إيقاف الدخول مؤقتًا بعد كثرة المحاولات غير الصحيحة. حاول مرة أخرى بعد",
                        "lockout_minutes": LOCKOUT_MINUTES,
                        "lockout_unit": "دقيقة.",
                        "csrf_token": get_csrf_token(request),
                    },
                    status_code=429,
                )
            return templates.TemplateResponse(
                request=request, name="login.html",
                context={"error": "اسم المستخدم أو كلمة المرور غير صحيحة.", "csrf_token": get_csrf_token(request)},
                status_code=401,
            )
        if _post_restore_passphrase_armed(db):
            # Disaster-restore identity proof: passwords alone came from the
            # archive, so the first sign-in must also present the offline
            # backup passphrase. Guesses share the sensitive-action throttle.
            from app.backup_crypto import unwrap_data_key_for_restore
            if reauth_lockout_remaining(db) is not None:
                return templates.TemplateResponse(
                    request=request, name="login.html",
                    context={"error": "أُوقفت هذه العملية مؤقتًا بعد كثرة المحاولات. حاول لاحقًا.",
                             "backup_passphrase_required": True,
                             "csrf_token": get_csrf_token(request)},
                    status_code=429,
                )
            try:
                unwrap_data_key_for_restore(db, backup_passphrase)
            except Exception:
                record_failed_reauth(db, actor="post_restore_login")
                return templates.TemplateResponse(
                    request=request, name="login.html",
                    context={"error": "كلمة مرور النسخ الاحتياطية غير صحيحة.",
                             "backup_passphrase_required": True,
                             "csrf_token": get_csrf_token(request)},
                    status_code=401,
                )
            clear_reauth_attempts(db, actor="post_restore_login")
            db.merge(SystemSetting(key="post_restore_passphrase_required", value="", updated_by=admin.username))
            db.add(AuditLog(
                actor=admin.username,
                action="post_restore_passphrase_verified",
                entity_type="admin_account",
                entity_id=admin.username,
                reason="Backup passphrase presented at first post-restore sign-in.",
            ))
        clear_attempts(db, actor="login")
        staged_token = request.session.get("security_roster_stage")
        if isinstance(staged_token, str):
            _clear_staged_security_roster(staged_token, request)
        session_id = secrets.token_hex(16)
        db.add(AdminSession(
            id=session_id,
            admin_account_id=admin.id,
            user_agent=(request.headers.get("user-agent") or "")[:300] or None,
            ip_address=request.client.host if request.client else None,
        ))
        db.commit()
        request.session.clear()
        request.session["admin_username"] = admin.username
        request.session["admin_session_version"] = admin.session_version
        request.session["admin_session_id"] = session_id
        get_csrf_token(request)
    send_login_alert("login_success", admin.username)
    send_telegram_login_event("login_success", admin.username, app_name=settings.app_name)
    if admin.must_change_password:
        destination = "/settings/password?forced=1"
    else:
        destination = "/attendance/import" if admin.role == ROLE_ASSISTANT else "/"
    return RedirectResponse(destination, status_code=303)


@app.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request):
    session_id = request.session.get("admin_session_id")
    if isinstance(session_id, str) and session_id:
        with SessionLocal.begin() as db:
            db.execute(delete(AdminSession).where(AdminSession.id == session_id))
    staged_token = request.session.get("security_roster_stage")
    if isinstance(staged_token, str):
        _clear_staged_security_roster(staged_token, request)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, admin: AdminAccount = Depends(get_current_admin)):
    if admin.role == ROLE_ASSISTANT:
        # The dashboard carries payroll/advance counters; the assistant
        # manager's home is attendance instead.
        return RedirectResponse("/attendance/import", status_code=303)
    with SessionLocal() as db:
        metrics = {
            "active_employees": db.scalar(
                select(func.count(Employee.id)).where(Employee.status == "active")
            ) or 0,
            "former_employees": db.scalar(
                select(func.count(Employee.id)).where(Employee.status != "active")
            ) or 0,
            "attendance_review": db.scalar(
                select(func.count(AttendanceDailyRecord.id)).where(
                    AttendanceDailyRecord.status.in_(["needs_review", "ready_for_review"])
                )
            ) or 0,
            "leave_drafts": db.scalar(
                select(func.count(LeaveRecord.id)).where(LeaveRecord.status == "draft")
            ) or 0,
            "advance_pending": db.scalar(
                select(func.count(AdvanceRequest.id)).where(
                    AdvanceRequest.status.in_(["pending", "approved"])
                )
            ) or 0,
            "staff_action_drafts": db.scalar(
                select(func.count(StaffAction.id)).where(StaffAction.status == "draft")
            ) or 0,
            "payroll_drafts": db.scalar(
                select(func.count(PayrollRun.id)).where(
                    PayrollRun.status.in_(["draft", "reopened"])
                )
            ) or 0,
        }
        alerts = []
        if admin.role == "system_admin":
            backup_root = get_backup_root(db)
            if backup_root is None:
                alerts.append({
                    "pre": "لم يتم ضبط موقع النسخ الاحتياطي على قرص خارجي بعد.",
                    "value": None, "post": "",
                    "link": "/settings/backup", "link_label": "فتح النسخ الاحتياطي ←",
                })
            last_success = db.scalars(
                select(BackupRun).where(BackupRun.status == "success")
                .order_by(BackupRun.created_at.desc(), BackupRun.id.desc()).limit(1)
            ).first()
            if backup_root is not None and last_success is None:
                alerts.append({
                    "pre": "لا توجد نسخة احتياطية ناجحة بعد.",
                    "value": None, "post": "",
                    "link": "/settings/backup", "link_label": "فتح النسخ الاحتياطي ←",
                })
            elif last_success is not None:
                created = last_success.created_at
                if created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                days = (datetime.now(timezone.utc) - created).days
                if days >= 7:
                    alerts.append({
                        "pre": "آخر نسخة احتياطية ناجحة منذ",
                        "value": days,
                        "post": "أيام." if days <= 10 else "يومًا.",
                        "link": "/settings/backup", "link_label": "فتح النسخ الاحتياطي ←",
                    })
            last_run = db.scalars(
                select(BackupRun).order_by(BackupRun.created_at.desc(), BackupRun.id.desc()).limit(1)
            ).first()
            if last_run is not None and last_run.status == "failed":
                alerts.append({
                    "pre": "فشلت آخر محاولة نسخ احتياطي؛ راجع صفحة النسخ الاحتياطي.",
                    "value": None, "post": "",
                    "link": "/settings/backup", "link_label": "فتح النسخ الاحتياطي ←",
                })
        # Six-month mini-charts (approved UI batch): posted payroll totals and
        # approved attended days per month. Month-scoped counts keep this
        # portable and cheap.
        def _month_add(d: date, n: int) -> date:
            m = d.month - 1 + n
            return date(d.year + m // 12, m % 12 + 1, 1)

        month_zero = date.today().replace(day=1)
        chart_payroll = []
        chart_attendance = []
        for i in range(-5, 1):
            m_start = _month_add(month_zero, i)
            m_end = _month_add(month_zero, i + 1)
            payroll_total = db.scalar(
                select(func.coalesce(func.sum(PayrollItem.net_pay), 0))
                .select_from(PayrollItem)
                .join(PayrollRun, PayrollRun.id == PayrollItem.payroll_run_id)
                .where(
                    PayrollRun.status.in_(["approved", "closed"]),
                    PayrollRun.period_start >= m_start,
                    PayrollRun.period_start < m_end,
                )
            ) or 0
            attended_days = db.scalar(
                select(func.count(AttendanceDailyRecord.id)).where(
                    AttendanceDailyRecord.status == "approved",
                    AttendanceDailyRecord.worked_minutes > 0,
                    AttendanceDailyRecord.work_date >= m_start,
                    AttendanceDailyRecord.work_date < m_end,
                )
            ) or 0
            label = m_start.strftime("%Y-%m")
            chart_payroll.append({"label": label, "value": float(payroll_total)})
            chart_attendance.append({"label": label, "value": int(attended_days)})
        charts = {
            "payroll": chart_payroll,
            "attendance": chart_attendance,
            "payroll_max": max((c["value"] for c in chart_payroll), default=0) or 1,
            "attendance_max": max((c["value"] for c in chart_attendance), default=0) or 1,
        }
        missing_fingerprint = db.scalar(
            select(func.count(Employee.id)).where(
                Employee.status == "active",
                (Employee.fingerprint_id.is_(None)) | (Employee.fingerprint_id == ""),
            )
        ) or 0
        if missing_fingerprint:
            alerts.append({
                "pre": "موظفون نشطون بدون رقم بصمة:",
                "value": missing_fingerprint, "post": "",
                "link": "/employees", "link_label": "فتح الموظفين ←",
            })
        return templates.TemplateResponse(
            request=request, name="dashboard.html",
            context={
                "app_name": settings.app_name,
                "admin": admin.display_name or admin.username,
                "csrf_token": get_csrf_token(request),
                "metrics": metrics,
                "alerts": alerts,
                "charts": charts,
            },
        )


@app.get("/ai", response_class=HTMLResponse)
def ai_page(request: Request, admin: AdminAccount = Depends(require_system_admin)):
    return templates.TemplateResponse(
        request=request,
        name="ai.html",
        context={
            "admin": admin.display_name or admin.username,
            "csrf_token": get_csrf_token(request),
            "status": local_model_status(request.state.language),
            "scan": build_smart_scan(request.state.language),
            "question": "",
            "answer": None,
            "memory": recent_memory(admin.username),
            "ui_language": request.state.language,
        },
    )


@app.post("/ai", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def ai_ask(
    request: Request,
    question: str = Form(...),
    admin: AdminAccount = Depends(require_system_admin),
):
    question = question.strip()
    if not question or len(question) > 2000:
        raise HTTPException(status_code=400, detail=_translate_text("اكتب أمرًا أو سؤالًا من 1 إلى 2000 حرف.", request.state.language))

    pending = None
    answer = None
    answer_language = request.state.language
    # Fetch the thread before the gateway persists this turn, so the template
    # does not render the current exchange twice (once from memory, once as
    # the live question/answer bubbles).
    history = recent_memory(admin.username)
    if is_command_request(question):
        try:
            result = prepare_command(question, admin.username, language=request.state.language)
            pending = {
                "token": result["token"],
                "summary": result["summary"],
                "plan": result["plan"],
                "old_values": result.get("old_values", {}),
                "new_values": result.get("new_values", {}),
                "risks": result.get("risks", []),
                "expires_at": result["expires_at"].isoformat(),
            }
            answer = _translate_text("تم إعداد خطة التنفيذ. لن يتم أي تعديل قبل موافقتك الصريحة.", request.state.language)
        except (RuntimeError, ValueError, SQLAlchemyError) as exc:
            answer = _translate_text("لم يتم إعداد العملية", request.state.language) + f": {exc}"
            answer_language = "ar"  # raw suffix may come from deeper Arabic layers
    else:
        with SessionLocal() as db:
            context = {
                "metrics": {
                    "active_employees": db.scalar(select(func.count(Employee.id)).where(Employee.status == "active")) or 0,
                    "former_employees": db.scalar(select(func.count(Employee.id)).where(Employee.status != "active")) or 0,
                    "attendance_review": db.scalar(select(func.count(AttendanceDailyRecord.id)).where(
                        AttendanceDailyRecord.status.in_(["needs_review", "ready_for_review"])
                    )) or 0,
                    "leave_drafts": db.scalar(select(func.count(LeaveRecord.id)).where(LeaveRecord.status == "draft")) or 0,
                    "advance_pending": db.scalar(select(func.count(AdvanceRequest.id)).where(
                        AdvanceRequest.status.in_(["pending", "approved"])
                    )) or 0,
                    "payroll_drafts": db.scalar(select(func.count(PayrollRun.id)).where(
                        PayrollRun.status.in_(["draft", "reopened"])
                    )) or 0,
                }
            }
        if is_structured_hr_query(question):
            try:
                answer = answer_structured_hr_query(question, language=request.state.language)
            except (RuntimeError, ValueError, SQLAlchemyError) as exc:
                answer = _translate_text("تعذر قراءة بيانات الموارد البشرية المطلوبة", request.state.language) + f": {exc}"
        else:
            try:
                answer = ask_local_model(question, context, language=request.state.language)
            except RuntimeError:
                answer = local_fallback_answer(question, context, language=request.state.language)

    if answer is not None and pending is None:
        record_exchange(admin.username, question, answer, language=answer_language)
    return templates.TemplateResponse(
        request=request,
        name="ai.html",
        context={
            "admin": admin.display_name or admin.username,
            "csrf_token": get_csrf_token(request),
            "status": local_model_status(request.state.language),
            "scan": build_smart_scan(request.state.language),
            "question": question,
            "answer": answer,
            "answer_language": answer_language,
            "pending": pending,
            "memory": history,
            "ui_language": request.state.language,
        },
    )


@app.post("/ai/approve", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def ai_approve(
    request: Request,
    token: str = Form(...),
    admin: AdminAccount = Depends(require_system_admin),
):
    history = recent_memory(admin.username)
    try:
        answer = approve_command(token.strip(), admin.username, language=request.state.language)
        # structured execution results render fully in the UI language
        answer_language = request.state.language
    except (RuntimeError, ValueError, SQLAlchemyError) as exc:
        # translated prefix + original (Arabic) error detail: mixed, no badge
        answer = _translate_text("لم يتم تنفيذ العملية", request.state.language) + f": {exc}"
        answer_language = "unknown"
    return templates.TemplateResponse(
        request=request,
        name="ai.html",
        context={
            "admin": admin.display_name or admin.username,
            "csrf_token": get_csrf_token(request),
            "status": local_model_status(request.state.language),
            "scan": build_smart_scan(request.state.language),
            "question": "",
            "answer": answer,
            "answer_language": answer_language,
            "pending": None,
            "memory": history,
            "ui_language": request.state.language,
        },
    )


@app.post("/ai/reject", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def ai_reject(
    request: Request,
    token: str = Form(...),
    admin: AdminAccount = Depends(require_system_admin),
):
    history = recent_memory(admin.username)
    try:
        answer = reject_command(token.strip(), admin.username, language=request.state.language)
        answer_language = request.state.language
    except (RuntimeError, ValueError, SQLAlchemyError) as exc:
        # translated prefix + original (Arabic) error detail: mixed, no badge
        answer = _translate_text("لم يتم رفض العملية", request.state.language) + f": {exc}"
        answer_language = "unknown"
    return templates.TemplateResponse(
        request=request,
        name="ai.html",
        context={
            "admin": admin.display_name or admin.username,
            "csrf_token": get_csrf_token(request),
            "status": local_model_status(request.state.language),
            "scan": build_smart_scan(request.state.language),
            "question": "",
            "answer": answer,
            "answer_language": answer_language,
            "pending": None,
            "memory": history,
            "ui_language": request.state.language,
        },
    )


@app.get("/security-roster", response_class=HTMLResponse)
def security_roster_page(
    request: Request,
    admin: AdminAccount = Depends(require_attendance_read),
):
    # Item 86: simple list of the confirmed security shifts for the coming
    # week under the upload form; read-only, no workflow change.
    # Established business timezone convention: CAIRO_TZ (as used app-wide).
    week_start = datetime.now(CAIRO_TZ).date()
    week_end = week_start + timedelta(days=7)
    with SessionLocal() as db:
        upcoming = db.execute(
            select(SecurityShiftAssignment, Employee, ShiftPattern)
            .join(Employee, Employee.id == SecurityShiftAssignment.employee_id)
            .join(ShiftPattern, ShiftPattern.id == SecurityShiftAssignment.shift_pattern_id)
            .where(SecurityShiftAssignment.work_date >= week_start)
            .where(SecurityShiftAssignment.work_date <= week_end)
            .order_by(SecurityShiftAssignment.work_date, ShiftPattern.start_time, Employee.full_name)
        ).all()
    upcoming_days = []
    for assignment, employee, pattern in upcoming:
        if not upcoming_days or upcoming_days[-1]["work_date"] != assignment.work_date:
            upcoming_days.append({"work_date": assignment.work_date, "rows": []})
        upcoming_days[-1]["rows"].append({
            "employee_name": employee.full_name,
            "employee_code": employee.code,
            "pattern_name": pattern.name,
            "start_time": pattern.start_time,
            "end_time": pattern.end_time,
        })
    return templates.TemplateResponse(
        request=request,
        name="security_roster.html",
        context={
            "admin": admin.display_name or admin.username,
            "csrf_token": get_csrf_token(request),
            "days": [],
            "issues": [],
            "error": None,
            "filename": None,
            "upcoming_days": upcoming_days,
            "upcoming_count": len(upcoming),
            "week_start": week_start,
            "week_end": week_end,
        },
    )


def _stage_security_roster(raw: bytes, request: Request) -> str:
    SECURITY_ROSTER_STAGING_DIR.mkdir(parents=True, exist_ok=True)
    try:
        SECURITY_ROSTER_STAGING_DIR.chmod(0o700)
    except OSError:
        pass
    now = time_module.time()
    _cleanup_expired_security_roster_files(now)
    token = secrets.token_urlsafe(32)
    path = SECURITY_ROSTER_STAGING_DIR / f"{token}.xlsx"
    path.write_bytes(raw)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    request.session["security_roster_stage"] = token
    return token


def _load_staged_security_roster(token: str, request: Request) -> bytes:
    expected = request.session.get("security_roster_stage")
    if not isinstance(expected, str) or not secrets.compare_digest(expected, token):
        raise HTTPException(status_code=403, detail="انتهت صلاحية معاينة الملف. ارفع الملف مرة أخرى.")
    if not token or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in token):
        raise HTTPException(status_code=400, detail="رمز المعاينة غير صالح.")
    path = SECURITY_ROSTER_STAGING_DIR / f"{token}.xlsx"
    try:
        if time_module.time() - path.stat().st_mtime > SECURITY_ROSTER_STAGING_TTL_SECONDS:
            path.unlink(missing_ok=True)
            raise HTTPException(status_code=410, detail="انتهت صلاحية معاينة الملف. ارفع الملف مرة أخرى.")
        raw = path.read_bytes()
    except FileNotFoundError:
        raise HTTPException(status_code=410, detail="ملف المعاينة غير موجود. ارفع الملف مرة أخرى.")
    if len(raw) > SECURITY_ROSTER_MAX_BYTES:
        path.unlink(missing_ok=True)
        raise HTTPException(status_code=413, detail="حجم ملف جدول الأمن غير صالح.")
    return raw


def _clear_staged_security_roster(token: str, request: Request) -> None:
    if request.session.get("security_roster_stage") == token:
        request.session.pop("security_roster_stage", None)
    if token and all(ch in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in token):
        (SECURITY_ROSTER_STAGING_DIR / f"{token}.xlsx").unlink(missing_ok=True)


def _read_security_roster_upload(upload: UploadFile) -> bytes:
    filename = (upload.filename or "").strip()
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail="يرجى رفع ملف Excel بصيغة .xlsx.")
    raw = upload.file.read(SECURITY_ROSTER_MAX_BYTES + 1)
    if not raw:
        raise HTTPException(status_code=400, detail="الملف فارغ.")
    if len(raw) > SECURITY_ROSTER_MAX_BYTES:
        raise HTTPException(status_code=413, detail="حجم ملف جدول الأمن يجب ألا يتجاوز 5 ميجابايت.")
    return raw


def _security_roster_aliases(db):
    from sqlalchemy import select
    from app.models import Department, Employee
    department = db.scalar(select(Department).where(
        Department.name == "Security", Department.active.is_(True)
    ))
    if department is None:
        raise ValueError("security_department_not_configured")
    employees = db.scalars(select(Employee).where(
        Employee.department_id == department.id,
        Employee.status == "active",
    ).order_by(Employee.id)).all()
    if len(employees) < 2:
        raise ValueError("security_department_must_have_at_least_two_active_employees")
    aliases = {}
    normalized_owners = {}
    for employee in employees:
        for value in (employee.full_name, employee.code, employee.fingerprint_id):
            if not value:
                continue
            key = " ".join(str(value).casefold().split())
            owner = normalized_owners.get(key)
            if owner is not None and owner != employee.id:
                raise ValueError("security_employee_identifiers_are_ambiguous_for_excel_mapping")
            normalized_owners[key] = employee.id
            aliases[str(value)] = employee.id
    if len({value for value in aliases.values()}) < 2:
        raise ValueError("security_employee_identifiers_must_resolve_to_at_least_two_employees")
    return aliases


def _attendance_employee_aliases(db):
    employees = db.scalars(select(Employee).order_by(Employee.id)).all()
    aliases = {}
    owners = {}
    for employee in employees:
        for value in (employee.code, employee.fingerprint_id, employee.full_name):
            if not value:
                continue
            key = normalize_attendance_alias(value)
            owner = owners.get(key)
            if owner is not None and owner != employee.id:
                raise ValueError("employee_identifiers_are_ambiguous_for_attendance_import")
            owners[key] = employee.id
            aliases[str(value)] = employee.id
    if not aliases:
        raise ValueError("no_employee_identifiers_configured")
    return aliases


def _read_attendance_upload(upload: UploadFile) -> bytes:
    filename = (upload.filename or "").strip()
    if Path(filename).suffix.casefold() not in {".xls", ".xlsx", ".csv"}:
        raise HTTPException(status_code=400, detail="يرجى رفع ملف حضور بصيغة CSV أو XLS أو XLSX.")
    raw = upload.file.read(SECURITY_ROSTER_MAX_BYTES + 1)
    if not raw:
        raise HTTPException(status_code=400, detail="الملف فارغ.")
    if len(raw) > SECURITY_ROSTER_MAX_BYTES:
        raise HTTPException(status_code=413, detail="حجم ملف الحضور يجب ألا يتجاوز 5 ميجابايت.")
    return raw


@app.get("/attendance/import/{batch_id}/download")
def attendance_import_download(
    batch_id: int,
    admin: AdminAccount = Depends(require_attendance_read),
):
    """Return the exact original workbook retained with an attendance import batch."""
    with SessionLocal() as db:
        batch = db.get(AttendanceImportBatch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="ملف الاستيراد غير موجود.")
        if not batch.original_file_bytes:
            raise HTTPException(
                status_code=410,
                detail="الملف الأصلي لهذه الدفعة لم يُحفظ في النسخة القديمة من النظام.",
            )
        suffix = Path(batch.original_filename).suffix.casefold()
        media_type = (
            "text/csv; charset=utf-8" if suffix == ".csv"
            else "application/vnd.ms-excel" if suffix == ".xls"
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        from urllib.parse import quote
        filename = quote(Path(batch.original_filename).name)
        return Response(
            content=bytes(batch.original_file_bytes),
            media_type=media_type,
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
        )


@app.get("/attendance/import", response_class=HTMLResponse)
def attendance_import_page(
    request: Request,
    admin: AdminAccount = Depends(require_attendance_read),
):
    return templates.TemplateResponse(
        request=request,
        name="attendance_import.html",
        context={"admin": admin.display_name or admin.username, "csrf_token": get_csrf_token(request),
                 "error": None, "batch": None, "issues": []},
    )


@app.post("/attendance/import", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def attendance_import_upload(
    request: Request,
    source_type: str = Form(...),
    file: UploadFile = File(...),
    admin: AdminAccount = Depends(require_attendance_write),
):
    context = {
        "admin": admin.display_name or admin.username, "csrf_token": get_csrf_token(request),
        "error": None, "batch": None, "issues": [],
        "issue_labels": {
            "unknown_employee": "رقم أو اسم الموظف غير معروف",
            "invalid_or_missing_date": "تاريخ مفقود أو غير صالح",
            "invalid_time_value": "وقت بصمة غير صالح",
            "duplicate_punch": "بصمة مكررة تم استبعادها",
            "invalid_row_column_count": "عدد أعمدة الصف لا يطابق عناوين الملف",
            "duplicate_date": "تاريخ مكرر",
            "missing_day_columns": "أعمدة الأيام لا تغطي الفترة المطلوبة",
        },
    }
    try:
        if source_type not in {"program_report", "fingerprint"}:
            raise ValueError("unsupported_attendance_source_type")
        raw = _read_attendance_upload(file)
        with SessionLocal.begin() as db:
            aliases = _attendance_employee_aliases(db)
            parsed = parse_attendance_workbook(raw, file.filename or "attendance.xlsx", source_type, aliases)
            batch = persist_attendance_import(
                db, file_bytes=raw, filename=file.filename or "attendance.xlsx",
                parsed=parsed, actor=admin.username,
            )
            context.update({
                "batch": {
                    "id": batch.id, "source_type": batch.source_type,
                    "period_start": batch.period_start, "period_end": batch.period_end,
                    "row_count": batch.row_count, "issue_count": batch.issue_count,
                    "status": batch.status,
                },
                "issues": parsed["issues"],
            })
    except (ValueError, KeyError) as exc:
        messages = {
            "empty_attendance_file": "الملف فارغ.",
            "unsupported_attendance_file_extension": "صيغة الملف غير مدعومة. استخدم CSV أو XLS أو XLSX.",
            "csv_encoding_not_supported": "تعذر قراءة ترميز ملف CSV. احفظه بترميز UTF-8 أو UTF-8 مع BOM أو Windows-1256.",
            "csv_delimiter_not_detected": "تعذر تحديد الفاصل بين أعمدة CSV.",
            "invalid_csv_file": "ملف CSV غير سليم أو يحتوي على علامات اقتباس غير مكتملة.",
            "attendance_headers_not_found": "لم يتم العثور على عناوين أعمدة الحضور المطلوبة.",
            "attendance_import_has_no_valid_dated_events": "لا يحتوي الملف على أحداث حضور بتاريخ صالح.",
        }
        context["error"] = messages.get(str(exc), str(exc))
        return templates.TemplateResponse(
            request=request, name="attendance_import.html", context=context, status_code=400
        )
    return templates.TemplateResponse(
        request=request, name="attendance_import.html", context=context
    )


def _attendance_import_batch_options():
    with SessionLocal() as db:
        return db.scalars(
            select(AttendanceImportBatch)
            .where(AttendanceImportBatch.status == "parsed")
            .order_by(AttendanceImportBatch.created_at.desc(), AttendanceImportBatch.id.desc())
            .limit(50)
        ).all()


@app.get("/attendance/import/{batch_id}/issues", response_class=HTMLResponse)
def attendance_import_issues_page(
    request: Request,
    batch_id: int,
    admin: AdminAccount = Depends(require_attendance_read),
):
    with SessionLocal() as db:
        batch = db.get(AttendanceImportBatch, batch_id)
        if batch is None:
            raise HTTPException(status_code=404, detail="دفعة الاستيراد غير موجودة.")
        issues = db.scalars(
            select(AttendanceImportIssue)
            .where(AttendanceImportIssue.import_batch_id == batch_id)
            .order_by(AttendanceImportIssue.source_row, AttendanceImportIssue.id)
        ).all()
        employees = db.scalars(select(Employee).order_by(Employee.full_name, Employee.id)).all()
    return templates.TemplateResponse(
        request=request,
        name="attendance_import_issues.html",
        context={
            "admin": admin.display_name or admin.username, "csrf_token": get_csrf_token(request),
            "batch": batch, "issues": issues, "employees": employees, "error": None,
        },
    )


@app.post(
    "/attendance/import/{batch_id}/issues/{issue_id}/resolve",
    response_class=HTMLResponse,
    dependencies=[Depends(csrf_protect)],
)
def attendance_import_issue_resolve(
    request: Request,
    batch_id: int,
    issue_id: int,
    employee_id: int = Form(...),
    admin: AdminAccount = Depends(require_attendance_write),
):
    try:
        with SessionLocal.begin() as db:
            issue = db.get(AttendanceImportIssue, issue_id)
            if issue is None or issue.import_batch_id != batch_id:
                raise ValueError("attendance_import_issue_not_found")
            resolve_unknown_attendance_employee(
                db, issue_id=issue_id, employee_id=employee_id, actor=admin.username
            )
    except ValueError as exc:
        with SessionLocal() as db:
            batch = db.get(AttendanceImportBatch, batch_id)
            issues = db.scalars(
                select(AttendanceImportIssue)
                .where(AttendanceImportIssue.import_batch_id == batch_id)
                .order_by(AttendanceImportIssue.source_row, AttendanceImportIssue.id)
            ).all() if batch else []
            employees = db.scalars(select(Employee).order_by(Employee.full_name, Employee.id)).all()
        return templates.TemplateResponse(
            request=request,
            name="attendance_import_issues.html",
            context={
                "admin": admin.display_name or admin.username, "csrf_token": get_csrf_token(request),
                "batch": batch, "issues": issues, "employees": employees,
                "error": str(exc),
            },
            status_code=400,
        )
    return RedirectResponse(f"/attendance/import/{batch_id}/issues", status_code=303)


@app.get("/attendance/reconcile", response_class=HTMLResponse)
def attendance_reconcile_page(
    request: Request,
    admin: AdminAccount = Depends(require_attendance_read),
):
    batches = _attendance_import_batch_options()
    return templates.TemplateResponse(
        request=request,
        name="attendance_reconcile.html",
        context={
            "admin": admin.display_name or admin.username, "csrf_token": get_csrf_token(request),
            "batches": batches, "error": None, "summary": None,
        },
    )


@app.post("/attendance/reconcile", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def attendance_reconcile_upload(
    request: Request,
    program_batch_id: int = Form(...),
    fingerprint_batch_id: int = Form(...),
    period_start: str = Form(...),
    period_end: str = Form(...),
    admin: AdminAccount = Depends(require_attendance_write),
):
    context = {
        "admin": admin.display_name or admin.username, "csrf_token": get_csrf_token(request),
        "batches": _attendance_import_batch_options(), "error": None, "summary": None,
    }
    try:
        selected_start = date.fromisoformat(period_start)
        selected_end = date.fromisoformat(period_end)
        with SessionLocal.begin() as db:
            summary = prepare_and_persist_monthly_reconciliation(
                db,
                program_batch_id=program_batch_id,
                fingerprint_batch_id=fingerprint_batch_id,
                period_start=selected_start,
                period_end=selected_end,
                actor=admin.username,
            )
        context["summary"] = summary
    except (ValueError, KeyError) as exc:
        context["error"] = str(exc)
        return templates.TemplateResponse(
            request=request, name="attendance_reconcile.html",
            context=context, status_code=400,
        )
    return templates.TemplateResponse(
        request=request, name="attendance_reconcile.html", context=context
    )


@app.get("/attendance/review", response_class=HTMLResponse)
def attendance_review_page(
    request: Request,
    page: int = 1,
    admin: AdminAccount = Depends(require_attendance_read),
):
    page_size = 100
    page = max(1, page)
    pending_filter = AttendanceDailyRecord.status.in_(["ready_for_review", "needs_review"])
    with SessionLocal() as db:
        total_records = db.scalar(
            select(func.count()).select_from(AttendanceDailyRecord).where(pending_filter)
        ) or 0
        total_pages = max(1, (total_records + page_size - 1) // page_size)
        page = min(page, total_pages)
        rows = db.execute(
            select(AttendanceDailyRecord, Employee)
            .join(Employee, Employee.id == AttendanceDailyRecord.employee_id)
            .where(pending_filter)
            .order_by(AttendanceDailyRecord.work_date, Employee.full_name, AttendanceDailyRecord.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        status_counts = dict(
            db.execute(
                select(AttendanceDailyRecord.status, func.count())
                .where(pending_filter)
                .group_by(AttendanceDailyRecord.status)
            ).all()
        )
        records = []
        for record, employee in rows:
            # Manual HR evidence is the latest explicit correction and must
            # be visible as the default review proposal, without replacing raw punches.
            suggested_in, suggested_out = suggested_clock_pair(record)
            records.append({
                "id": record.id, "employee": employee.full_name,
                "employee_code": employee.code, "work_date": record.work_date,
                "scheduled_start_at": record.scheduled_start_at,
                "scheduled_end_at": record.scheduled_end_at,
                "program_clock_in": record.program_clock_in,
                "program_clock_out": record.program_clock_out,
                "fingerprint_in": record.first_clock_in,
                "fingerprint_out": record.last_clock_out,
                "manual_clock_in": record.manual_clock_in,
                "manual_clock_out": record.manual_clock_out,
                "manual_entry_reason": record.manual_entry_reason,
                "manual_entered_by": record.manual_entered_by,
                "manual_entered_at": record.manual_entered_at,
                "suggested_clock_in": suggested_in,
                "suggested_clock_out": suggested_out,
                "worked_minutes": record.worked_minutes,
                "status": record.status, "review_notes": record.review_notes,
                "review_notes_display": _attendance_notes_display(record.review_notes, request.state.language),
            })
        # Item 52: the bulk action commits every ready record with a complete
        # suggested pair — the page lists that exact set with its times so the
        # reviewer inspects precisely what will be approved, and the digest
        # token is re-verified inside the locked approval transaction.
        bulk_pairs, bulk_scope_token = bulk_approval_scope(db)
        bulk_employee_ids = {record.employee_id for record, _, _ in bulk_pairs}
        bulk_employees = {
            e.id: e for e in db.scalars(
                select(Employee).where(Employee.id.in_(bulk_employee_ids))
            ).all()
        } if bulk_employee_ids else {}
        bulk_rows = [{
            "id": record.id,
            "employee": bulk_employees[record.employee_id].full_name if record.employee_id in bulk_employees else "—",
            "employee_code": bulk_employees[record.employee_id].code if record.employee_id in bulk_employees else "—",
            "work_date": record.work_date,
            "suggested_clock_in": clock_in,
            "suggested_clock_out": clock_out,
        } for record, clock_in, clock_out in bulk_pairs]
    employees = db.scalars(
        select(Employee).where(Employee.status == "active").order_by(Employee.full_name, Employee.id)
    ).all()
    return templates.TemplateResponse(
        request=request, name="attendance_review.html",
        context={"admin": admin.display_name or admin.username, "csrf_token": get_csrf_token(request),
                 "records": records, "employees": employees, "error": None, "page": page,
                 "total_pages": total_pages, "total_records": total_records,
                 "page_size": page_size,
                 "matching_count": len(bulk_rows),
                 "needs_review_count": status_counts.get("needs_review", 0),
                 "bulk_rows": bulk_rows,
                 "bulk_scope_token": bulk_scope_token,
                 "bulk_approved": request.query_params.get("bulk_approved"),
                 "bulk_skipped": request.query_params.get("bulk_skipped"),
                 "bulk_stale": request.query_params.get("bulk_stale")},
    )


@app.post("/attendance/manual", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def attendance_manual_entry(
    request: Request,
    employee_id: int = Form(...),
    work_date: str = Form(...),
    clock_in: str = Form(...),
    clock_out: str = Form(...),
    reason: str = Form(...),
    admin: AdminAccount = Depends(require_attendance_write),
):
    try:
        entered_date = date.fromisoformat(work_date)
        entered_in = datetime.fromisoformat(clock_in)
        entered_out = datetime.fromisoformat(clock_out)
        with SessionLocal.begin() as db:
            add_manual_attendance(
                db,
                employee_id=employee_id,
                work_date=entered_date,
                clock_in=entered_in,
                clock_out=entered_out,
                reason=reason,
                actor=admin.username,
            )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/attendance/review", status_code=303)


@app.post("/attendance/{record_id}/approve", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def attendance_approve(
    record_id: int,
    request: Request,
    clock_in: str = Form(...),
    clock_out: str = Form(...),
    reason: str = Form(...),
    admin: AdminAccount = Depends(require_attendance_write),
):
    try:
        approved_in = datetime.fromisoformat(clock_in)
        approved_out = datetime.fromisoformat(clock_out)
        with SessionLocal.begin() as db:
            approve_attendance_day(
                db, record_id=record_id, clock_in=approved_in,
                clock_out=approved_out, actor=admin.username, reason=reason,
            )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/attendance/review", status_code=303)


@app.post("/attendance/approve-all-matching", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def attendance_approve_all_matching(
    request: Request,
    reason: str = Form(...),
    scope_token: str = Form(default=""),
    admin: AdminAccount = Depends(require_attendance_write),
):
    if not scope_token.strip():
        # The bulk action is only valid against the exact set the reviewer saw
        # listed on the review page; a tokenless submission cannot prove that.
        return RedirectResponse("/attendance/review?bulk_stale=1", status_code=303)
    try:
        with SessionLocal.begin() as db:
            result = approve_all_matching_attendance(
                db, actor=admin.username, reason=reason,
                scope_token=scope_token,
            )
    except BulkScopeMismatch:
        # The ready set changed since the page was rendered: refuse the whole
        # action and ask for a fresh review instead of approving unseen times.
        return RedirectResponse("/attendance/review?bulk_stale=1", status_code=303)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse(
        f"/attendance/review?bulk_approved={result['approved']}&bulk_skipped={len(result['skipped'])}",
        status_code=303,
    )


@app.get("/attendance/months", response_class=HTMLResponse)
def attendance_months_page(
    request: Request,
    admin: AdminAccount = Depends(require_attendance_read),
):
    with SessionLocal() as db:
        months = db.scalars(
            select(AttendanceMonth).order_by(
                AttendanceMonth.period_start.desc(), AttendanceMonth.id.desc()
            )
        ).all()
        summaries = []
        for month in months:
            grouped = db.execute(
                select(AttendanceDailyRecord.status, func.count())
                .where(
                    AttendanceDailyRecord.work_date >= month.period_start,
                    AttendanceDailyRecord.work_date <= month.period_end,
                    AttendanceDailyRecord.program_import_batch_id == month.program_import_batch_id,
                    AttendanceDailyRecord.fingerprint_import_batch_id == month.fingerprint_import_batch_id,
                )
                .group_by(AttendanceDailyRecord.status)
            ).all()
            counts = {status: count for status, count in grouped}
            program = db.get(AttendanceImportBatch, month.program_import_batch_id) if month.program_import_batch_id else None
            fingerprint = db.get(AttendanceImportBatch, month.fingerprint_import_batch_id) if month.fingerprint_import_batch_id else None
            coverage = summarize_attendance_month_coverage(db, month)
            summaries.append({
                "id": month.id, "period_start": month.period_start,
                "period_end": month.period_end, "status": month.status,
                "total": sum(counts.values()),
                "approved": counts.get("approved", 0),
                "pending": sum(value for key, value in counts.items() if key != "approved"),
                "program_issues": program.issue_count if program else None,
                "fingerprint_issues": fingerprint.issue_count if fingerprint else None,
                "unrepresented_employee_days": coverage["unrepresented_employee_days"],
                "coverage_examples": coverage["coverage_examples"],
                "approved_by": month.approved_by,
            })
    return templates.TemplateResponse(
        request=request, name="attendance_months.html",
        context={"admin": admin.display_name or admin.username, "csrf_token": get_csrf_token(request),
                 "months": summaries, "error": None},
    )


@app.post("/attendance/months/{month_id}/approve", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def attendance_month_approve(
    month_id: int,
    request: Request,
    reason: str = Form(...),
    coverage_confirmed: bool = Form(False),
    admin: AdminAccount = Depends(require_attendance_write),
):
    try:
        with SessionLocal.begin() as db:
            approve_attendance_month(
                db, month_id=month_id, actor=admin.username,
                reason=reason, coverage_confirmed=coverage_confirmed,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RedirectResponse("/attendance/months", status_code=303)


@app.post("/security-roster/preview", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def security_roster_preview(
    request: Request,
    file: UploadFile = File(...),
    admin: AdminAccount = Depends(require_attendance_write),
):
    try:
        raw = _read_security_roster_upload(file)
        with SessionLocal() as db:
            aliases = _security_roster_aliases(db)
            parsed = parse_security_roster_xlsx(raw, aliases)
            prepared = prepare_security_roster(db, parsed)
        stage_token = _stage_security_roster(raw, request)
        return templates.TemplateResponse(
            request=request,
            name="security_roster.html",
            context={
                "admin": admin.display_name or admin.username,
                "csrf_token": get_csrf_token(request),
                "days": prepared["days"],
                "issues": prepared["import_issues"],
                "error": None,
                "filename": file.filename,
                "stage_token": stage_token,
            },
        )
    except (ValueError, KeyError) as exc:
        return templates.TemplateResponse(
            request=request,
            name="security_roster.html",
            context={
                "admin": admin.display_name or admin.username,
                "csrf_token": get_csrf_token(request),
                "days": [],
                "issues": [],
                "error": str(exc),
                "filename": file.filename,
            },
            status_code=400,
        )


@app.post("/security-roster/confirm", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def security_roster_confirm(
    request: Request,
    work_date: str = Form(...),
    stage_token: str = Form(...),
    reviewed: str = Form(""),
    admin: AdminAccount = Depends(require_attendance_write),
):
    from datetime import date
    if reviewed != "yes":
        raise HTTPException(status_code=400, detail="يجب تأكيد مراجعة ورديات اليوم قبل الاعتماد.")
    try:
        raw = _load_staged_security_roster(stage_token, request)
        selected_date = date.fromisoformat(work_date)
        with SessionLocal.begin() as db:
            aliases = _security_roster_aliases(db)
            parsed = parse_security_roster_xlsx(raw, aliases)
            prepared = prepare_security_roster(db, parsed)
            selected = next(
                (item for item in prepared["days"] if item["work_date"] == selected_date),
                None,
            )
            if selected is None or not selected["can_confirm"]:
                raise ValueError("security_roster_day_not_ready_for_approval")
            confirm_security_roster_day(
                db,
                selected_date,
                selected["roster"],
                admin,
                import_issues=parsed.get("issues", []),
            )
        # Keep the staged workbook available so the administrator can approve
        # each clean date independently; TTL cleanup removes it after one hour.
        # Rebuild the preview after commit so the remaining dates can be
        # approved from the same workbook without uploading it again.
        from app.models import SecurityShiftAssignment
        with SessionLocal() as db:
            aliases = _security_roster_aliases(db)
            refreshed = parse_security_roster_xlsx(raw, aliases)
            refreshed_preview = prepare_security_roster(db, refreshed)
            approved_dates = set(db.scalars(
                select(SecurityShiftAssignment.work_date).where(
                    SecurityShiftAssignment.work_date.in_(
                        [day["work_date"] for day in refreshed_preview["days"]]
                    )
                )
            ).all())
            for day in refreshed_preview["days"]:
                if day["work_date"] in approved_dates:
                    day["can_confirm"] = False
                    day["already_approved"] = True
        return templates.TemplateResponse(
            request=request,
            name="security_roster.html",
            context={
                "admin": admin.display_name or admin.username,
                "csrf_token": get_csrf_token(request),
                "days": refreshed_preview["days"],
                "issues": refreshed_preview["import_issues"],
                "error": None,
                "success": f"تم اعتماد جدول يوم {selected_date.isoformat()} بنجاح.",
                "filename": "ملف جدول الأمن",
                "stage_token": stage_token,
            },
        )
    except (ValueError, KeyError) as exc:
        return templates.TemplateResponse(
            request=request,
            name="security_roster.html",
            context={
                "admin": admin.display_name or admin.username,
                "csrf_token": get_csrf_token(request),
                "days": [],
                "issues": [],
                "error": str(exc),
                "filename": "ملف جدول الأمن",
            },
            status_code=400,
        )


@app.post("/departments", dependencies=[Depends(csrf_protect)])
def create_department(
    name: str = Form(...),
    admin: AdminAccount = Depends(require_system_admin),
):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="اسم القسم مطلوب.")
    with SessionLocal.begin() as db:
        if db.scalar(select(Department.id).where(Department.name == name)):
            raise HTTPException(status_code=409, detail="اسم القسم مستخدم بالفعل.")
        department = Department(name=name, active=True)
        db.add(department)
        db.flush()
        db.add(AuditLog(
            actor=admin.username, action="department_created",
            entity_type="department", entity_id=str(department.id),
            new_value=name, reason="Department created by HR",
        ))
    return RedirectResponse("/employees", status_code=303)


@app.post("/departments/{department_id}/edit", dependencies=[Depends(csrf_protect)])
def edit_department(
    department_id: int,
    name: str = Form(...),
    active: bool = Form(False),
    admin: AdminAccount = Depends(require_system_admin),
):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="اسم القسم مطلوب.")
    with SessionLocal.begin() as db:
        department = db.get(Department, department_id)
        if department is None:
            raise HTTPException(status_code=404, detail="القسم غير موجود.")
        duplicate = db.scalar(select(Department.id).where(
            Department.name == name, Department.id != department_id
        ))
        if duplicate:
            raise HTTPException(status_code=409, detail="اسم القسم مستخدم بالفعل.")
        if not active:
            active_employee = db.scalar(select(Employee.id).where(
                Employee.department_id == department_id, Employee.status == "active"
            ).limit(1))
            if active_employee is not None:
                raise HTTPException(status_code=409, detail="انقل الموظفين النشطين إلى قسم آخر قبل إيقاف هذا القسم.")
        old_value = f"name={department.name}; active={department.active}"
        department.name = name
        department.active = active
        db.add(AuditLog(
            actor=admin.username, action="department_updated",
            entity_type="department", entity_id=str(department.id),
            old_value=old_value, new_value=f"name={name}; active={active}",
            reason="Department settings updated",
        ))
    return RedirectResponse("/employees", status_code=303)


@app.post("/branches", dependencies=[Depends(csrf_protect)])
def create_branch(
    department_id: int = Form(...),
    name: str = Form(...),
    admin: AdminAccount = Depends(require_system_admin),
):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="اسم الفرع مطلوب.")
    with SessionLocal.begin() as db:
        department = db.get(Department, department_id)
        if department is None or not department.active:
            raise HTTPException(status_code=400, detail="القسم المحدد غير متاح.")
        duplicate = db.scalar(select(Branch.id).where(
            Branch.department_id == department_id, Branch.name == name
        ))
        if duplicate:
            raise HTTPException(status_code=409, detail="اسم الفرع مستخدم داخل هذا القسم.")
        branch = Branch(department_id=department_id, name=name, active=True)
        db.add(branch)
        db.flush()
        db.add(AuditLog(
            actor=admin.username, action="branch_created",
            entity_type="branch", entity_id=str(branch.id),
            new_value=f"{department.name}/{name}", reason="Branch created by HR",
        ))
    return RedirectResponse("/employees", status_code=303)


@app.post("/branches/{branch_id}/edit", dependencies=[Depends(csrf_protect)])
def edit_branch(
    branch_id: int,
    department_id: int = Form(...),
    name: str = Form(...),
    active: bool = Form(False),
    admin: AdminAccount = Depends(require_system_admin),
):
    name = name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="اسم الفرع مطلوب.")
    with SessionLocal.begin() as db:
        branch = db.get(Branch, branch_id)
        if branch is None:
            raise HTTPException(status_code=404, detail="الفرع غير موجود.")
        department = db.get(Department, department_id)
        if department is None or not department.active:
            raise HTTPException(status_code=400, detail="القسم المحدد غير متاح.")
        duplicate = db.scalar(select(Branch.id).where(
            Branch.department_id == department_id,
            Branch.name == name,
            Branch.id != branch_id,
        ))
        if duplicate:
            raise HTTPException(status_code=409, detail="اسم الفرع مستخدم داخل هذا القسم.")
        any_assignment = db.scalar(select(Employee.id).where(
            Employee.branch_id == branch_id
        ).limit(1))
        if any_assignment is not None and department_id != branch.department_id:
            raise HTTPException(status_code=409, detail="لا يمكن تغيير القسم التابع للفرع بعد ربط موظفين به؛ أنشئ فرعًا جديدًا للحفاظ على السجل التاريخي.")
        active_assignment = db.scalar(select(Employee.id).where(
            Employee.branch_id == branch_id, Employee.status == "active"
        ).limit(1))
        if not active and active_assignment is not None:
            raise HTTPException(status_code=409, detail="انقل الموظفين النشطين من هذا الفرع قبل إيقافه.")
        old_value = f"department_id={branch.department_id}; name={branch.name}; active={branch.active}"
        branch.department_id = department_id
        branch.name = name
        branch.active = active
        db.add(AuditLog(
            actor=admin.username, action="branch_updated",
            entity_type="branch", entity_id=str(branch.id),
            old_value=old_value,
            new_value=f"department_id={department_id}; name={name}; active={active}",
            reason="Branch settings updated",
        ))
    return RedirectResponse("/employees", status_code=303)


@app.get("/employees/{employee_id}/photo")
def employee_photo(
    employee_id: int,
    admin: AdminAccount = Depends(require_employee_read),
):
    """Serve an employee photo only to roles with employee-directory access."""
    with SessionLocal() as db:
        employee = db.get(Employee, employee_id)
        if employee is None or not employee.photo_path:
            raise HTTPException(status_code=404, detail="صورة الموظف غير موجودة.")

        # Only a stored basename is accepted; never allow a database value to
        # escape the private photo directory.
        filename = Path(employee.photo_path).name
        if filename != employee.photo_path:
            raise HTTPException(status_code=404, detail="صورة الموظف غير موجودة.")
        photo_path = EMPLOYEE_PHOTO_DIR / filename
        try:
            photo_path.resolve().relative_to(EMPLOYEE_PHOTO_DIR.resolve())
        except ValueError:
            raise HTTPException(status_code=404, detail="صورة الموظف غير موجودة.")
        if not photo_path.is_file():
            raise HTTPException(status_code=404, detail="صورة الموظف غير موجودة.")

        media_types = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }
        media_type = media_types.get(photo_path.suffix.lower())
        if media_type is None:
            raise HTTPException(status_code=404, detail="صيغة صورة الموظف غير مدعومة.")
        return FileResponse(
            photo_path,
            media_type=media_type,
            headers={
                "Cache-Control": "private, no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )


@app.get("/leave", response_class=HTMLResponse)
def leave_page(request: Request, page: int = 1, admin: AdminAccount = Depends(require_leave_read)):
    page_size = 100
    page = max(1, page)
    with SessionLocal() as db:
        employees = db.scalars(
            select(Employee).where(Employee.status == "active").order_by(Employee.code)
        ).all()
        leave_types = db.scalars(
            select(LeaveType).where(LeaveType.active.is_(True)).order_by(LeaveType.id)
        ).all()
        # Only the records table is paginated; balances and the latest
        # transactions below still cover the whole ledger.
        total_records = db.scalar(select(func.count()).select_from(LeaveRecord)) or 0
        total_pages = max(1, (total_records + page_size - 1) // page_size)
        page = min(page, total_pages)
        records = db.scalars(
            select(LeaveRecord).order_by(LeaveRecord.created_at.desc(), LeaveRecord.id.desc())
            .offset((page - 1) * page_size).limit(page_size)
        ).all()
        all_employees = db.scalars(select(Employee).order_by(Employee.code)).all()
        transactions = db.scalars(
            select(LeaveBalanceTransaction).order_by(
                LeaveBalanceTransaction.created_at.desc(),
                LeaveBalanceTransaction.id.desc(),
            ).limit(100)
        ).all()
        balance_rows = db.execute(
            select(
                LeaveBalanceTransaction.employee_id,
                LeaveBalanceTransaction.leave_type_id,
                func.sum(LeaveBalanceTransaction.days_delta),
            ).group_by(
                LeaveBalanceTransaction.employee_id,
                LeaveBalanceTransaction.leave_type_id,
            )
        ).all()
        balances = {
            f"{employee_id}:{leave_type_id}": Decimal(str(total or 0))
            for employee_id, leave_type_id, total in balance_rows
        }
        return templates.TemplateResponse(
            request=request,
            name="leave.html",
            context={
                "employees": employees,
                "all_employees": all_employees,
                "leave_types": leave_types,
                "records": records,
                "page": page, "total_pages": total_pages,
                "total_records": total_records, "records_page_size": page_size,
                "employee_names": {employee.id: employee.full_name for employee in all_employees},
                "employee_codes": {employee.id: employee.code for employee in all_employees},
                "type_names": {item.id: _leave_type_name(item, request.state.language) for item in db.scalars(select(LeaveType)).all()},
                "balances": balances,
                "transactions": transactions,
                "csrf_token": get_csrf_token(request),
                "error": request.query_params.get("error"),
            "employees": employees,
            },
        )


@app.post("/leave/types", dependencies=[Depends(csrf_protect)])
def create_leave_type(
    code: str = Form(...),
    name_ar: str = Form(...),
    name_en: str = Form(...),
    name_zh: str | None = Form(None),
    is_paid: str | None = Form(None),
    tracks_balance: str | None = Form(None),
    admin: AdminAccount = Depends(require_attendance_write),
):
    normalized_code = code.strip().lower()
    if not name_ar.strip() or not name_en.strip():
        return RedirectResponse("/leave?error=type", status_code=303)
    if not normalized_code or not normalized_code.replace("_", "").isalnum():
        return RedirectResponse("/leave?error=type", status_code=303)
    with SessionLocal.begin() as db:
        if db.scalar(select(LeaveType.id).where(LeaveType.code == normalized_code)):
            return RedirectResponse("/leave?error=type", status_code=303)
        item = LeaveType(
            code=normalized_code,
            name_ar=name_ar.strip(),
            name_en=name_en.strip(),
            name_zh=(name_zh or "").strip() or None,
            is_paid=is_paid == "yes",
            tracks_balance=tracks_balance == "yes",
            active=True,
        )
        db.add(item)
        db.flush()
        db.add(AuditLog(
            actor=admin.username, action="leave_type_created",
            entity_type="leave_type", entity_id=str(item.id),
            new_value=f"{normalized_code}|{name_ar.strip()}|{name_en.strip()}",
            reason="Created by HR administrator",
        ))
    return RedirectResponse("/leave", status_code=303)


@app.post("/leave/balances", dependencies=[Depends(csrf_protect)])
def record_leave_balance(
    employee_id: int = Form(...),
    leave_type_id: int = Form(...),
    transaction_type: str = Form(...),
    days_delta: Decimal = Form(...),
    reason: str = Form(...),
    admin: AdminAccount = Depends(require_attendance_write),
):
    if transaction_type not in {"opening", "adjustment"} or not reason.strip():
        return RedirectResponse("/leave?error=balance", status_code=303)
    try:
        with SessionLocal.begin() as db:
            employee = db.get(Employee, employee_id)
            leave_type = db.get(LeaveType, leave_type_id)
            if employee is None or employee.status != "active" or leave_type is None:
                raise ValueError("Invalid employee or leave type.")
            add_balance_entry(
                db, employee_id=employee_id, leave_type=leave_type,
                transaction_type=transaction_type, days_delta=days_delta,
                reason=reason, actor=admin.username,
            )
    except (ValueError, SQLAlchemyError):
        return RedirectResponse("/leave?error=balance", status_code=303)
    return RedirectResponse("/leave", status_code=303)


@app.post("/leave/records", dependencies=[Depends(csrf_protect)])
def create_leave_record(
    employee_id: int = Form(...),
    leave_type_id: int = Form(...),
    start_date: date = Form(...),
    end_date: date = Form(...),
    charged_days: Decimal = Form(...),
    reason: str = Form(...),
    admin: AdminAccount = Depends(require_attendance_write),
):
    if end_date < start_date or charged_days <= 0 or not reason.strip():
        return RedirectResponse("/leave?error=dates", status_code=303)
    calendar_days = (end_date - start_date).days + 1
    if charged_days > Decimal(calendar_days):
        return RedirectResponse("/leave?error=days", status_code=303)
    with SessionLocal.begin() as db:
        employee = db.get(Employee, employee_id)
        leave_type = db.get(LeaveType, leave_type_id)
        if employee is None or employee.status != "active" or leave_type is None or not leave_type.active:
            return RedirectResponse("/leave?error=employee", status_code=303)
        overlap = db.scalar(
            select(LeaveRecord.id).where(
                LeaveRecord.employee_id == employee_id,
                LeaveRecord.status != "cancelled",
                LeaveRecord.start_date <= end_date,
                LeaveRecord.end_date >= start_date,
            ).limit(1)
        )
        if overlap is not None:
            return RedirectResponse("/leave?error=overlap", status_code=303)
        # Planned leave needs at least 72 hours' notice. Sick and emergency leave
        # are exceptions, but the administrator must still enter a reason.
        starts_at = datetime.combine(start_date, datetime.min.time(), tzinfo=CAIRO_TZ)
        if leave_type.code not in {"sick", "casual"} and starts_at < datetime.now(CAIRO_TZ) + timedelta(hours=72):
            return RedirectResponse("/leave?error=notice", status_code=303)
        record = LeaveRecord(
            employee_id=employee_id,
            leave_type_id=leave_type_id,
            start_date=start_date,
            end_date=end_date,
            calendar_days=calendar_days,
            charged_days=charged_days,
            reason=reason.strip(),
            status="draft",
            created_by=admin.username,
        )
        db.add(record)
        db.flush()
        db.add(AuditLog(
            actor=admin.username, action="leave_record_created",
            entity_type="leave_record", entity_id=str(record.id),
            new_value=f"{start_date.isoformat()}|{end_date.isoformat()}|{charged_days}",
            reason=reason.strip(),
        ))
    return RedirectResponse("/leave", status_code=303)


@app.post("/leave/records/{record_id}/confirm", dependencies=[Depends(csrf_protect)])
def confirm_leave_record(
    record_id: int,
    admin: AdminAccount = Depends(require_attendance_write),
):
    try:
        with SessionLocal.begin() as db:
            record = db.scalar(
                select(LeaveRecord)
                .where(LeaveRecord.id == record_id)
                .with_for_update()
            )
            if record is None:
                return RedirectResponse("/leave?error=record", status_code=303)
            # Serialize balance-consuming approvals for the same employee.
            db.scalar(
                select(Employee)
                .where(Employee.id == record.employee_id)
                .with_for_update()
            )
            confirm_leave(db, record, admin.username)
    except (ValueError, SQLAlchemyError):
        return RedirectResponse("/leave?error=confirm", status_code=303)
    return RedirectResponse("/leave", status_code=303)


@app.post("/leave/records/{record_id}/cancel", dependencies=[Depends(csrf_protect)])
def cancel_leave_record(
    record_id: int,
    reason: str = Form(...),
    admin: AdminAccount = Depends(require_attendance_write),
):
    if not reason.strip():
        return RedirectResponse("/leave?error=cancel", status_code=303)
    try:
        with SessionLocal.begin() as db:
            record = db.get(LeaveRecord, record_id)
            if record is None:
                return RedirectResponse("/leave?error=record", status_code=303)
            cancel_leave(db, record, admin.username, reason)
    except (ValueError, SQLAlchemyError):
        return RedirectResponse("/leave?error=cancel", status_code=303)
    return RedirectResponse("/leave", status_code=303)


@app.get("/advances", response_class=HTMLResponse)
def advances_page(request: Request, page: int = 1, tab: str = "pending", admin: AdminAccount = Depends(require_payroll_access)):
    with SessionLocal() as db:
        employees = db.scalars(
            select(Employee).where(Employee.status == "active").order_by(Employee.code)
        ).all()
        all_employees = db.scalars(select(Employee).order_by(Employee.code)).all()
        # The per-employee summary must keep covering every request, so it
        # reads light columns for all rows plus one grouped installment sum
        # instead of a sum query per request. Only the table below pages.
        summary_rows = db.execute(
            select(
                AdvanceRequest.id, AdvanceRequest.employee_id,
                AdvanceRequest.amount, AdvanceRequest.advance_type,
                AdvanceRequest.status, AdvanceRequest.request_date,
            ).order_by(AdvanceRequest.request_date.desc(), AdvanceRequest.id.desc())
        ).all()
        applied_map = {
            request_id: Decimal(str(total or 0))
            for request_id, total in db.execute(
                select(
                    AdvanceInstallment.advance_request_id,
                    func.coalesce(func.sum(AdvanceInstallment.amount), 0),
                ).where(AdvanceInstallment.status == "applied")
                .group_by(AdvanceInstallment.advance_request_id)
            ).all()
        }
        page_size = 100
        page = max(1, page)
        # Item 53 tabs: pending / approved / all. Tab counts cover every
        # request regardless of the current page.
        if tab not in {"pending", "approved", "all"}:
            tab = "pending"
        tab_counts = dict(
            db.execute(
                select(AdvanceRequest.status, func.count()).group_by(AdvanceRequest.status)
            ).all()
        )
        list_query = select(AdvanceRequest)
        if tab != "all":
            list_query = list_query.where(AdvanceRequest.status == tab)
        total_requests = db.scalar(
            select(func.count()).select_from(AdvanceRequest).where(
                AdvanceRequest.status == tab if tab != "all" else True
            )
        ) or 0
        total_pages = max(1, (total_requests + page_size - 1) // page_size)
        page = min(page, total_pages)
        requests = db.scalars(
            list_query.order_by(
                AdvanceRequest.request_date.desc(), AdvanceRequest.id.desc()
            ).offset((page - 1) * page_size).limit(page_size)
        ).all()
        advance_summary = {}
        for employee in employees:
            employee_requests = [item for item in summary_rows if item.employee_id == employee.id]
            regular_outstanding = Decimal("0.00")
            installment_outstanding = Decimal("0.00")
            deducted = Decimal("0.00")
            for item in employee_requests:
                applied = applied_map.get(item.id, Decimal("0.00"))
                if item.status in {"pending", "approved", "paid"}:
                    remaining = Decimal(item.amount) - Decimal(applied)
                else:
                    remaining = Decimal("0.00")
                if item.advance_type == "regular":
                    regular_outstanding += max(Decimal("0.00"), remaining)
                else:
                    installment_outstanding += max(Decimal("0.00"), remaining)
                deducted += Decimal(applied)
            total = sum(
                (Decimal(item.amount) for item in employee_requests),
                Decimal("0.00"),
            )
            cap = Decimal(employee.basic_salary) * Decimal("0.25")
            advance_summary[employee.id] = {
                "latest": employee_requests[0] if employee_requests else None,
                "total": total,
                "deducted": deducted,
                "outstanding": regular_outstanding + installment_outstanding,
                "regular_outstanding": regular_outstanding,
                "installment_outstanding": installment_outstanding,
                "limit": cap,
                "available": max(Decimal("0.00"), cap - regular_outstanding),
            }
        for employee in employees:
            if advance_summary.get(employee.id) is None:
                default_limit = Decimal(str(employee.basic_salary)) * Decimal("0.25")
                advance_summary[employee.id] = {
                    "latest": None,
                    "total": Decimal("0.00"),
                    "deducted": Decimal("0.00"),
                    "outstanding": Decimal("0.00"),
                    "regular_outstanding": Decimal("0.00"),
                    "installment_outstanding": Decimal("0.00"),
                    "limit": default_limit,
                    "available": default_limit,
                }
        _advances_pager = {
            "page": page, "total_pages": total_pages,
            "total_records": total_requests, "records_page_size": page_size,
        }
        return templates.TemplateResponse(
            request=request,
            name="advances.html",
            context={**_advances_pager,
                "employees": employees,
                "requests": requests,
                "tab": tab,
                "tab_counts": tab_counts,
                "advance_summary": advance_summary,
                "employee_names": {e.id: e.full_name for e in all_employees},
                "employee_codes": {e.id: e.code for e in all_employees},
                "category_labels": CATEGORY_LABELS_AR,
                "today": date.today().isoformat(),
                "submission_key": uuid4().hex,
                "csrf_token": get_csrf_token(request),
                "error": request.query_params.get("error"),
            },
        )


@app.post("/advances", dependencies=[Depends(csrf_protect)])
def create_advance_request(
    employee_id: int = Form(...),
    request_date: date = Form(...),
    amount: Decimal = Form(...),
    advance_type: str = Form("regular"),
    reason: str = Form(...),
    submission_key: str = Form(..., min_length=32, max_length=80),
    admin: AdminAccount = Depends(require_payroll_access),
):
    try:
        with SessionLocal.begin() as db:
            create_advance(
                db,
                employee_id=employee_id,
                request_date=request_date,
                amount=amount,
                reason=reason,
                advance_type=advance_type,
                actor=admin.username,
                submission_key=submission_key,
            )
    except (ValueError, SQLAlchemyError):
        return RedirectResponse("/advances?error=request", status_code=303)
    return RedirectResponse("/advances", status_code=303)


@app.post("/advances/{request_id}/approve", dependencies=[Depends(csrf_protect)])
def approve_advance_request(
    request_id: int,
    installment_count: int = Form(1),
    admin: AdminAccount = Depends(require_payroll_access),
):
    try:
        with SessionLocal.begin() as db:
            request = db.scalar(
                select(AdvanceRequest)
                .where(AdvanceRequest.id == request_id)
                .with_for_update()
            )
            if request is None:
                return RedirectResponse("/advances?error=record", status_code=303)
            approval_installment_count = 1 if request.advance_type == "regular" else installment_count
            approve_advance(db, request, admin.username, installment_count=approval_installment_count)
    except (ValueError, SQLAlchemyError):
        return RedirectResponse("/advances?error=approve", status_code=303)
    return RedirectResponse("/advances", status_code=303)


@app.post("/advances/{request_id}/reject", dependencies=[Depends(csrf_protect)])
def reject_advance_request(
    request_id: int,
    reason: str = Form(...),
    admin: AdminAccount = Depends(require_payroll_access),
):
    try:
        with SessionLocal.begin() as db:
            request = db.scalar(
                select(AdvanceRequest)
                .where(AdvanceRequest.id == request_id)
                .with_for_update()
            )
            if request is None:
                return RedirectResponse("/advances?error=record", status_code=303)
            reject_advance(db, request, admin.username, reason)
    except (ValueError, SQLAlchemyError):
        return RedirectResponse("/advances?error=reject", status_code=303)
    return RedirectResponse("/advances", status_code=303)


@app.post("/advances/{request_id}/paid", dependencies=[Depends(csrf_protect)])
def mark_advance_request_paid(
    request_id: int,
    admin: AdminAccount = Depends(require_payroll_access),
):
    try:
        with SessionLocal.begin() as db:
            request = db.scalar(
                select(AdvanceRequest)
                .where(AdvanceRequest.id == request_id)
                .with_for_update()
            )
            if request is None:
                return RedirectResponse("/advances?error=record", status_code=303)
            mark_advance_paid(db, request, admin.username)
    except (ValueError, SQLAlchemyError):
        return RedirectResponse("/advances?error=paid", status_code=303)
    return RedirectResponse("/advances", status_code=303)


@app.get("/staff-actions", response_class=HTMLResponse)
def staff_actions_page(request: Request, page: int = 1, admin: AdminAccount = Depends(require_payroll_access)):
    page_size = 100
    page = max(1, page)
    with SessionLocal() as db:
        employees = db.scalars(
            select(Employee).where(Employee.status == "active").order_by(Employee.code)
        ).all()
        all_employees = db.scalars(select(Employee).order_by(Employee.code)).all()
        total_records = db.scalar(select(func.count()).select_from(StaffAction)) or 0
        total_pages = max(1, (total_records + page_size - 1) // page_size)
        page = min(page, total_pages)
        actions = db.scalars(
            select(StaffAction).order_by(StaffAction.created_at.desc(), StaffAction.id.desc())
            .offset((page - 1) * page_size).limit(page_size)
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="staff_actions.html",
            context={
                "employees": employees,
                "actions": actions,
                "page": page, "total_pages": total_pages,
                "total_records": total_records, "records_page_size": page_size,
                "employee_names": {e.id: e.full_name for e in all_employees},
                "employee_codes": {e.id: e.code for e in all_employees},
                "category_labels": CATEGORY_LABELS_AR,
                "csrf_token": get_csrf_token(request),
                "error": request.query_params.get("error"),
            },
        )


@app.post("/staff-actions", dependencies=[Depends(csrf_protect)])
def create_staff_action_route(
    employee_id: int = Form(...),
    kind: str = Form(...),
    category: str = Form(...),
    title: str = Form(...),
    is_financial: str | None = Form(None),
    amount: Decimal | None = Form(None),
    payroll_month: date | None = Form(None),
    reason: str = Form(...),
    admin: AdminAccount = Depends(require_payroll_access),
):
    try:
        with SessionLocal.begin() as db:
            create_staff_action(
                db,
                employee_id=employee_id,
                kind=kind,
                category=category,
                title=title,
                is_financial=is_financial == "yes",
                amount=amount,
                payroll_month=payroll_month,
                reason=reason,
                actor=admin.username,
            )
    except (ValueError, SQLAlchemyError):
        return RedirectResponse("/staff-actions?error=create", status_code=303)
    return RedirectResponse("/staff-actions", status_code=303)


@app.post("/staff-actions/{action_id}/approve", dependencies=[Depends(csrf_protect)])
def approve_staff_action_route(
    action_id: int,
    admin: AdminAccount = Depends(require_payroll_access),
):
    try:
        with SessionLocal.begin() as db:
            item = db.scalar(
                select(StaffAction)
                .where(StaffAction.id == action_id)
                .with_for_update()
            )
            if item is None:
                return RedirectResponse("/staff-actions?error=record", status_code=303)
            approve_staff_action(db, item, admin.username)
    except (ValueError, SQLAlchemyError):
        return RedirectResponse("/staff-actions?error=approve", status_code=303)
    return RedirectResponse("/staff-actions", status_code=303)


@app.post("/staff-actions/{action_id}/cancel", dependencies=[Depends(csrf_protect)])
def cancel_staff_action_route(
    action_id: int,
    reason: str = Form(...),
    admin: AdminAccount = Depends(require_payroll_access),
):
    try:
        with SessionLocal.begin() as db:
            item = db.scalar(
                select(StaffAction)
                .where(StaffAction.id == action_id)
                .with_for_update()
            )
            if item is None:
                return RedirectResponse("/staff-actions?error=record", status_code=303)
            cancel_staff_action(db, item, admin.username, reason)
    except (ValueError, SQLAlchemyError):
        return RedirectResponse("/staff-actions?error=cancel", status_code=303)
    return RedirectResponse("/staff-actions", status_code=303)


@app.post("/staff-actions/{action_id}/acknowledgement", dependencies=[Depends(csrf_protect)])
def staff_action_acknowledgement_route(
    action_id: int,
    status: str = Form(...),
    note: str = Form(""),
    admin: AdminAccount = Depends(require_payroll_access),
):
    try:
        with SessionLocal.begin() as db:
            item = db.scalar(
                select(StaffAction)
                .where(StaffAction.id == action_id)
                .with_for_update()
            )
            if item is None:
                return RedirectResponse("/staff-actions?error=record", status_code=303)
            record_employee_acknowledgement(
                db, item, admin.username, status, note
            )
    except (ValueError, SQLAlchemyError):
        return RedirectResponse("/staff-actions?error=ack", status_code=303)
    return RedirectResponse("/staff-actions", status_code=303)


@app.get("/payroll", response_class=HTMLResponse)
def payroll_page(request: Request, admin: AdminAccount = Depends(require_payroll_access)):
    with SessionLocal() as db:
        runs = db.scalars(
            select(PayrollRun).order_by(PayrollRun.period_start.desc())
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="payroll.html",
            context={
                "runs": runs,
                "csrf_token": get_csrf_token(request),
                "error": request.query_params.get("error"),
            },
        )


@app.post("/payroll", dependencies=[Depends(csrf_protect)])
def create_payroll_run_route(
    year: int = Form(...),
    month: int = Form(...),
    admin: AdminAccount = Depends(require_payroll_access),
):
    try:
        with SessionLocal.begin() as db:
            run = create_payroll_run(db, year=year, month=month, actor=admin.username)
            run_id = run.id
    except (ValueError, SQLAlchemyError):
        return RedirectResponse("/payroll?error=create", status_code=303)
    return RedirectResponse(f"/payroll/runs/{run_id}", status_code=303)


@app.get("/payroll/runs/{run_id}", response_class=HTMLResponse)
def payroll_run_page(
    run_id: int,
    request: Request,
    admin: AdminAccount = Depends(require_payroll_access),
):
    with SessionLocal() as db:
        run = db.get(PayrollRun, run_id)
        if run is None:
            return RedirectResponse("/payroll?error=run", status_code=303)
        items = db.scalars(
            select(PayrollItem)
            .where(PayrollItem.payroll_run_id == run.id)
            .order_by(PayrollItem.employee_id)
        ).all()
        employee_ids = [item.employee_id for item in items]
        employees = db.scalars(
            select(Employee).where(Employee.id.in_(employee_ids or {-1}))
        ).all()
        employee_names = {e.id: e.full_name for e in employees}
        employee_codes = {e.id: e.code for e in employees}
        summaries = {item.id: payment_totals(db, item) for item in items}
        financials = {}
        for item in items:
            gross = (
                Decimal(item.base_salary)
                + Decimal(item.attendance_bonus)
                + Decimal(item.performance_bonus)
                + Decimal(item.occupancy_bonus)
                + Decimal(item.overtime_amount)
                + Decimal(item.other_bonus)
                + Decimal(item.staff_reward_amount)
            )
            deductions = (
                Decimal(item.absence_deduction)
                + Decimal(item.lateness_deduction)
                + Decimal(item.insurance_share)
                + Decimal(item.taxes)
                + Decimal(item.other_deductions)
                + Decimal(item.advance_deduction)
                + Decimal(item.staff_sanction_amount)
            )
            financials[item.id] = {"gross": gross, "deductions": deductions}
        payments = db.scalars(
            select(PayrollPayment)
            .where(PayrollPayment.payroll_item_id.in_([item.id for item in items] or {-1}))
            .order_by(PayrollPayment.paid_at.desc(), PayrollPayment.id.desc())
        ).all()
        # Protection 1: absence deduction vs confirmed paid leave (review gate).
        conflict_map = {}
        if run.status in {"draft", "reopened"}:
            for item in items:
                conflicts = absence_leave_conflicts(db, item, run)
                if conflicts:
                    conflict_map[item.id] = conflicts
        # Protection 2: warn when the item snapshot no longer matches the
        # employee's effective compensation for the period (entered after the
        # draft was created), or when a mid-period change now exists.
        from app.payroll_workflow import _employee_snapshot_on_date
        stale_codes = []
        if run.status in {"draft", "reopened"}:
            for item in items:
                employee = db.get(Employee, item.employee_id)
                if employee is None:
                    continue
                try:
                    snapshot, changes = _employee_snapshot_on_date(db, employee, run.period_start)
                except ValueError:
                    stale_codes.append(item.employee_code_snapshot)
                    continue
                if (
                    Decimal(snapshot["basic_salary"]) != Decimal(item.salary_basis)
                    or Decimal(snapshot["daily_hours"]) != Decimal(item.daily_hours)
                    or any(
                        change.field_name in {"basic_salary", "daily_hours"}
                        and run.period_start < change.effective_date <= run.period_end
                        for change in changes
                    )
                ):
                    stale_codes.append(item.employee_code_snapshot)
        # Missing-employee alert: an active service period in this month with
        # no payroll item in the run (e.g. an employee entered after the draft
        # was created). Alert only; inclusion is an HR decision.
        from app.payroll_math import money
        missing_employees = []
        if run.status in {"draft", "reopened"}:
            item_employee_ids = {item.employee_id for item in items}
            active_periods = db.scalars(
                select(EmployeeServicePeriod).where(
                    EmployeeServicePeriod.start_date <= run.period_end,
                    or_(
                        EmployeeServicePeriod.end_date.is_(None),
                        EmployeeServicePeriod.end_date >= run.period_start,
                    ),
                )
            ).all()
            missing_ids = sorted({p.employee_id for p in active_periods} - item_employee_ids)
            for missing_id in missing_ids:
                employee = db.get(Employee, missing_id)
                if employee is not None:
                    missing_employees.append(f"{employee.code} — {employee.full_name}")
        zero = money(Decimal("0.00"))
        totals = {
            "count": len(items),
            "gross": money(sum(financials[item.id]["gross"] for item in items)) if items else zero,
            "deductions": money(sum(financials[item.id]["deductions"] for item in items)) if items else zero,
            "net": money(sum(Decimal(item.net_pay) for item in items)) if items else zero,
            "paid": money(sum(summaries[item.id][0] for item in items)) if items else zero,
            "due": money(sum(summaries[item.id][1] for item in items)) if items else zero,
        }
        # What-if simulation (GET, read-only): transient copies recomputed by
        # the payroll engine; nothing is written, approved or audited.
        from app.payroll_simulation import (
            ADJUSTABLE_FIELDS, FIELD_LABELS, simulate_run_adjustment,
        )
        sim_params = {
            "field": request.query_params.get("sim_field") or "",
            "mode": request.query_params.get("sim_mode") or "add",
            "value": request.query_params.get("sim_value") or "",
            "employee": request.query_params.get("sim_employee") or "",
        }
        payments_by_item = {}
        for payment in payments:
            payments_by_item.setdefault(payment.payroll_item_id, []).append(payment)
        simulation = None
        simulation_error = False
        if sim_params["field"]:
            try:
                employee_ids = [int(sim_params["employee"])] if sim_params["employee"] else None
                simulation = simulate_run_adjustment(
                    db, run.id,
                    field=sim_params["field"], mode=sim_params["mode"],
                    value=sim_params["value"], employee_ids=employee_ids,
                )
            except (ValueError, TypeError):
                simulation_error = True
        return templates.TemplateResponse(
            request=request,
            name="payroll_run.html",
            context={
                "run": run,
                "items": items,
                "employee_names": employee_names,
                "employee_codes": employee_codes,
                "summaries": summaries,
                "financials": financials,
                "payments": payments,
                "payments_by_item": payments_by_item,
                "conflict_map": conflict_map,
                "stale_codes": stale_codes,
                "missing_employees": missing_employees,
                "totals": totals,
                "csrf_token": get_csrf_token(request),
                "error": request.query_params.get("error"),
                "simulation": simulation,
                "simulation_error": simulation_error,
                "sim_params": sim_params,
                "sim_field_labels": FIELD_LABELS,
                "sim_fields": sorted(ADJUSTABLE_FIELDS, key=lambda f: FIELD_LABELS[f]),
            },
        )


@app.get("/payroll/runs/{run_id}/items/{item_id}/payslip.pdf")
def payroll_item_payslip(
    run_id: int,
    item_id: int,
    request: Request,
    admin: AdminAccount = Depends(require_payroll_access),
):
    from app.payslip import build_payslip_pdf

    with SessionLocal() as db:
        run = db.get(PayrollRun, run_id)
        item = db.get(PayrollItem, item_id)
        if run is None or item is None or item.payroll_run_id != run.id:
            raise HTTPException(status_code=404, detail="قسيمة المرتب غير موجودة.")
        totals = payment_totals(db, item)
        pdf = build_payslip_pdf(run, item, totals, datetime.now(timezone.utc), language=request.state.language)
        filename = f"payslip-{item.employee_code_snapshot}-{run.period_start.isoformat()}.pdf"
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


@app.post("/payroll/runs/{run_id}/approve", dependencies=[Depends(csrf_protect)])
def approve_payroll_run_route(
    run_id: int,
    admin: AdminAccount = Depends(require_payroll_access),
):
    try:
        with SessionLocal.begin() as db:
            run = db.scalar(
                select(PayrollRun).where(PayrollRun.id == run_id).with_for_update()
            )
            if run is None:
                return RedirectResponse("/payroll?error=run", status_code=303)
            approve_payroll_run(db, run, admin.username)
    except AbsenceLeaveConflictError:
        return RedirectResponse(
            f"/payroll/runs/{run_id}?error=absence_leave_conflict", status_code=303
        )
    except (ValueError, SQLAlchemyError):
        return RedirectResponse(f"/payroll/runs/{run_id}?error=approve", status_code=303)
    return RedirectResponse(f"/payroll/runs/{run_id}", status_code=303)


@app.post("/payroll/items/{item_id}/absence-leave-review", dependencies=[Depends(csrf_protect)])
def absence_leave_review_route(
    item_id: int,
    admin: AdminAccount = Depends(require_payroll_access),
):
    with SessionLocal() as db:
        item = db.get(PayrollItem, item_id)
        if item is None:
            return RedirectResponse("/payroll?error=run", status_code=303)
        run_id = item.payroll_run_id
    try:
        with SessionLocal.begin() as db:
            review_absence_leave_conflict(db, db.get(PayrollItem, item_id), admin.username)
    except (ValueError, SQLAlchemyError):
        return RedirectResponse(f"/payroll/runs/{run_id}?error=item", status_code=303)
    return RedirectResponse(f"/payroll/runs/{run_id}", status_code=303)


@app.post("/payroll/runs/{run_id}/close", dependencies=[Depends(csrf_protect)])
def close_payroll_run_route(
    run_id: int,
    admin: AdminAccount = Depends(require_payroll_access),
):
    try:
        with SessionLocal.begin() as db:
            run = db.scalar(
                select(PayrollRun).where(PayrollRun.id == run_id).with_for_update()
            )
            if run is None:
                return RedirectResponse("/payroll?error=run", status_code=303)
            close_payroll_run(db, run, admin.username)
    except (ValueError, SQLAlchemyError):
        return RedirectResponse(f"/payroll/runs/{run_id}?error=close", status_code=303)
    return RedirectResponse(f"/payroll/runs/{run_id}", status_code=303)


@app.post("/payroll/runs/{run_id}/reopen", dependencies=[Depends(csrf_protect)])
def reopen_payroll_run_route(
    run_id: int,
    reason: str = Form(...),
    admin: AdminAccount = Depends(require_payroll_access),
):
    try:
        with SessionLocal.begin() as db:
            run = db.scalar(
                select(PayrollRun).where(PayrollRun.id == run_id).with_for_update()
            )
            if run is None:
                return RedirectResponse("/payroll?error=run", status_code=303)
            reopen_payroll_run(db, run, admin.username, reason)
    except (ValueError, SQLAlchemyError):
        return RedirectResponse(f"/payroll/runs/{run_id}?error=reopen", status_code=303)
    return RedirectResponse(f"/payroll/runs/{run_id}", status_code=303)


@app.post("/payroll/items/{item_id}/update", dependencies=[Depends(csrf_protect)])
def update_payroll_item_route(
    item_id: int,
    attendance_bonus: Decimal = Form(Decimal("0")),
    performance_bonus: Decimal = Form(Decimal("0")),
    occupancy_bonus: Decimal = Form(Decimal("0")),
    overtime_hours: Decimal = Form(Decimal("0")),
    other_bonus: Decimal = Form(Decimal("0")),
    absence_days: Decimal = Form(Decimal("0")),
    lateness_deduction: Decimal = Form(Decimal("0")),
    insurance_share: Decimal = Form(Decimal("0")),
    taxes: Decimal = Form(Decimal("0")),
    other_deductions: Decimal = Form(Decimal("0")),
    note: str = Form(""),
    admin: AdminAccount = Depends(require_payroll_access),
):
    run_id = None
    try:
        with SessionLocal.begin() as db:
            item = db.scalar(
                select(PayrollItem).where(PayrollItem.id == item_id).with_for_update()
            )
            if item is None:
                return RedirectResponse("/payroll?error=item", status_code=303)
            run_id = item.payroll_run_id
            update_payroll_item(
                db,
                item,
                {
                    "attendance_bonus": attendance_bonus,
                    "performance_bonus": performance_bonus,
                    "occupancy_bonus": occupancy_bonus,
                    "overtime_hours": overtime_hours,
                    "other_bonus": other_bonus,
                    "absence_days": absence_days,
                    "lateness_deduction": lateness_deduction,
                    "insurance_share": insurance_share,
                    "taxes": taxes,
                    "other_deductions": other_deductions,
                    "note": note,
                },
                admin.username,
            )
    except (ValueError, SQLAlchemyError):
        return RedirectResponse(f"/payroll/runs/{run_id}?error=item", status_code=303)
    return RedirectResponse(f"/payroll/runs/{run_id}", status_code=303)


@app.post("/payroll/items/{item_id}/overtime-approve", dependencies=[Depends(csrf_protect)])
def approve_overtime_route(
    item_id: int,
    admin: AdminAccount = Depends(require_payroll_access),
):
    run_id = None
    try:
        with SessionLocal.begin() as db:
            item = db.scalar(
                select(PayrollItem).where(PayrollItem.id == item_id).with_for_update()
            )
            if item is None:
                return RedirectResponse("/payroll?error=item", status_code=303)
            run_id = item.payroll_run_id
            approve_overtime(db, item, admin.username)
    except (ValueError, SQLAlchemyError):
        return RedirectResponse(f"/payroll/runs/{run_id}?error=overtime", status_code=303)
    return RedirectResponse(f"/payroll/runs/{run_id}", status_code=303)


@app.post("/payroll/items/{item_id}/payments", dependencies=[Depends(csrf_protect)])
def add_payroll_payment_route(
    item_id: int,
    amount: Decimal = Form(...),
    paid_date: date = Form(...),
    method: str = Form(...),
    reference: str = Form(""),
    admin: AdminAccount = Depends(require_payroll_access),
):
    run_id = None
    try:
        with SessionLocal.begin() as db:
            item = db.scalar(
                select(PayrollItem).where(PayrollItem.id == item_id).with_for_update()
            )
            if item is None:
                return RedirectResponse("/payroll?error=item", status_code=303)
            run_id = item.payroll_run_id
            add_payroll_payment(
                db,
                item,
                amount=amount,
                paid_date=paid_date,
                method=method,
                reference=reference,
                actor=admin.username,
            )
    except (ValueError, SQLAlchemyError):
        return RedirectResponse(f"/payroll/runs/{run_id}?error=payment", status_code=303)
    return RedirectResponse(f"/payroll/runs/{run_id}", status_code=303)


@app.post("/payroll/payments/{payment_id}/correct", dependencies=[Depends(csrf_protect)])
def correct_payroll_payment_route(
    payment_id: int,
    corrected_amount: Decimal = Form(...),
    reason: str = Form(...),
    admin: AdminAccount = Depends(require_payroll_access),
):
    run_id = None
    try:
        with SessionLocal.begin() as db:
            payment = db.scalar(
                select(PayrollPayment).where(PayrollPayment.id == payment_id).with_for_update()
            )
            if payment is None:
                return RedirectResponse("/payroll?error=payment", status_code=303)
            # Lock the payroll item too, in the same order as the add-payment
            # route, so a correction can never interleave with a new payment
            # on the same item and both read a stale paid total.
            item = db.scalar(
                select(PayrollItem).where(PayrollItem.id == payment.payroll_item_id).with_for_update()
            )
            run_id = item.payroll_run_id if item is not None else None
            correct_payroll_payment(
                db,
                payment,
                corrected_amount=corrected_amount,
                reason=reason,
                actor=admin.username,
            )
    except (ValueError, SQLAlchemyError):
        return RedirectResponse(f"/payroll/runs/{run_id}?error=payment_correction", status_code=303)
    return RedirectResponse(f"/payroll/runs/{run_id}", status_code=303)


@app.get("/reports", response_class=HTMLResponse)
def reports_page(
    request: Request,
    admin: AdminAccount = Depends(get_current_admin),
):
    params = request.query_params
    selection = ReportSelection()
    active_preset = None
    with SessionLocal() as db:
        preset_id = None
        try:
            preset_id = int(params.get("preset_id", "") or 0) or None
        except ValueError:
            preset_id = None
        if preset_id is not None:
            active_preset = load_preset(db, preset_id)
        if params.get("report_type") or params.get("period") or params.get("start_date"):
            # A submitted request form (or the view page's edit link) repopulates everything.
            selection = selection_from_params(params)
            _assistant_default_fields(params, selection, admin)
        elif active_preset is not None:
            selection = ReportSelection.from_config(preset_config(active_preset))
        elif admin.role == ROLE_ASSISTANT:
            selection.report_type = REPORT_TYPE_ATTENDANCE
            selection.fields = set(_ATTENDANCE_SAFE_FIELDS)
        elif admin.role == ROLE_FINANCE:
            selection.report_type = REPORT_TYPE_PAYROLL
        _enforce_report_scope(selection, admin)
        start, end = resolve_period(selection)
        employees = db.scalars(select(Employee).order_by(Employee.code)).all()
        departments = db.scalars(select(Department).order_by(Department.name)).all()
        presets = list_presets(db)
        allowed_types = _REPORT_TYPES_BY_ROLE.get(admin.role)
        if allowed_types is not None:
            presets = [
                preset for preset in presets
                if preset_config(preset).get("report_type") in allowed_types
            ]
    # Item 87: library cards — one per ready report type for this role, each
    # running the report with this month's default selection; plus the newest
    # saved export copies with download links. The custom builder, monthly
    # PDF and presets below stay exactly as they are.
    allowed_for_role = [
        report_type for report_type in REPORT_TYPES
        if report_type in _REPORT_TYPES_BY_ROLE.get(admin.role, set(REPORT_TYPES))
    ]
    today = datetime.now(CAIRO_TZ).date()
    month_start = today.replace(day=1)
    month_end = today.replace(day=monthrange(today.year, today.month)[1])
    library_cards = []
    for report_type in allowed_for_role:
        if report_type == REPORT_TYPE_COMPREHENSIVE:
            # No preview exists for the comprehensive report: the card links
            # the working direct Excel export of this month, labeled as such.
            run_url = (
                f"/reports/export.xlsx?report_type={report_type}"
                f"&start_date={month_start.isoformat()}&end_date={month_end.isoformat()}"
            )
            action = "تصدير Excel مباشر"
        else:
            run_url = f"/reports/view?report_type={report_type}&period=this_month"
            action = "توليد التقرير"
        library_cards.append({
            "type": report_type,
            "label": "مجاميع لكل مكون" if report_type == REPORT_TYPE_PAYROLL else REPORT_TYPE_LABELS[report_type],
            "description": _REPORT_LIBRARY_DESCRIPTIONS.get(report_type, ""),
            "run_url": run_url,
            "action": action,
        })
    latest_files = _list_latest_report_files()
    return templates.TemplateResponse(
        request=request,
        name="reports.html",
        context={
            "selection": selection,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "employees": employees,
            "departments": departments,
            "presets": presets,
            "active_preset": active_preset,
            "field_group_defs": FIELD_GROUP_DEFS,
            "field_groups_by_type": FIELD_GROUPS_BY_TYPE,
            "report_types": [
                report_type for report_type in REPORT_TYPES
                if report_type in _REPORT_TYPES_BY_ROLE.get(admin.role, set(REPORT_TYPES))
            ],
            "report_type_labels": REPORT_TYPE_LABELS,
            "monthly_option_labels": MONTHLY_OPTION_LABELS,
            "monthly_allowed_options": sorted(monthly_options_for_types(_monthly_allowed_types(admin))),
            "csrf_token": get_csrf_token(request),
            "error": params.get("error"),
            "saved": params.get("saved"),
            "deleted": params.get("deleted"),
            "library_cards": library_cards,
            "latest_files": latest_files,
            "can_download_files": admin.role not in _REPORT_TYPES_BY_ROLE,
        },
    )


_REPORT_LIBRARY_DESCRIPTIONS = {
    REPORT_TYPE_PAYROLL: "مجاميع الرواتب لكل مكون، مع تفاصيل الموظفين مجمعة حسب القسم",
    REPORT_TYPE_ATTENDANCE: "سجلات الحضور والانصراف والغياب والإضافي",
    REPORT_TYPE_ADVANCES: "طلبات السلف وحالاتها وأقساط السداد",
    REPORT_TYPE_LEAVE: "أرصدة الإجازات وسجلاتها",
    REPORT_TYPE_STAFF_ACTIONS: "الجزاءات والمكافآت المسجلة",
    REPORT_TYPE_COMPREHENSIVE: "كل الأقسام والبيانات في ملف واحد",
}


def _list_latest_report_files(limit: int = 10) -> list[dict]:
    """Newest saved export copies (written on every xlsx/pdf export) for the
    reports library's latest-files list; newest first, names only — contents
    are served by the guarded download route."""
    try:
        entries = [
            entry for entry in REPORTS_DIR.iterdir()
            if entry.is_file() and (
                _REPORT_FILE_NAME_RE.match(entry.name) or _REPORT_FILE_NAME_LEGACY_RE.match(entry.name)
            )
        ]
    except OSError:
        return []
    entries.sort(key=lambda entry: entry.stat().st_mtime, reverse=True)
    files = []
    for entry in entries[:limit]:
        try:
            stat = entry.stat()
        except OSError:
            continue
        # The name pattern is anchored: MY_HR_Report_<start>_<end>_…, so the
        # date offsets are safe for a friendly display label.
        files.append({
            "name": entry.name,
            "kind": (
                "monthly" if "_monthly_" in entry.name
                else "custom" if _REPORT_FILE_NAME_RE.match(entry.name)
                else "legacy"
            ),
            "start": entry.name[13:23],
            "end": entry.name[24:34],
            "ext": entry.name.rsplit(".", 1)[-1].upper(),
            "size_kb": max(1, stat.st_size // 1024),
            "modified": datetime.fromtimestamp(stat.st_mtime),
        })
    return files


_REPORT_FILE_NAME_RE = re.compile(
    r"^MY_HR_Report_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}_(employee-\d+|all-employees)_\d{8}-\d{6}-\d+_(custom|monthly)_?[0-9a-f]{8}\.(xlsx|pdf)$"
)
# Pre-kind filenames (before the kind segment existed) are still listed and
# downloadable; they just carry a generic label.
_REPORT_FILE_NAME_LEGACY_RE = re.compile(
    r"^MY_HR_Report_\d{4}-\d{2}-\d{2}_\d{4}-\d{2}-\d{2}_(employee-\d+|all-employees)_\d{8}-\d{6}-\d+_[0-9a-f]{8}\.(xlsx|pdf)$"
)


@app.get("/reports/files/{filename}")
def reports_download_saved_file(
    filename: str,
    admin: AdminAccount = Depends(get_current_admin),
):
    """Item 87: download a saved export copy from the latest-files list.
    A copy may carry any section the generator's role allowed, so only roles
    with unrestricted report access may download: a restricted role (finance,
    assistant_manager) could otherwise read sections outside its scope. The
    name pattern and path containment check block traversal."""
    if admin.role in _REPORT_TYPES_BY_ROLE:
        raise HTTPException(status_code=403, detail="هذا الجزء غير متاح لصلاحية حسابك.")
    if not (_REPORT_FILE_NAME_RE.match(filename) or _REPORT_FILE_NAME_LEGACY_RE.match(filename)):
        raise HTTPException(status_code=404, detail="الملف غير موجود.")
    path = (REPORTS_DIR / filename).resolve()
    if REPORTS_DIR.resolve() not in path.parents or not path.is_file():
        raise HTTPException(status_code=404, detail="الملف غير موجود.")
    media_type = "application/pdf" if filename.endswith(".pdf") else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return FileResponse(path, media_type=media_type, filename=filename)


@app.get("/reports/view", response_class=HTMLResponse)
def view_hr_report(
    request: Request,
    admin: AdminAccount = Depends(get_current_admin),
):
    selection = selection_from_params(request.query_params)
    _assistant_default_fields(request.query_params, selection, admin)
    _enforce_report_scope(selection, admin)
    if selection.report_type == REPORT_TYPE_COMPREHENSIVE:
        return RedirectResponse(f"/reports?error=comprehensive&{request.url.query}", status_code=303)
    try:
        with SessionLocal() as db:
            result = build_report(db, selection)
    except ValueError:
        return RedirectResponse(f"/reports?error=dates&{request.url.query}", status_code=303)
    except SQLAlchemyError:
        return RedirectResponse(f"/reports?error=report&{request.url.query}", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="report_view.html",
        context={
            "result": result,
            "query_string": request.url.query,
            "breakdown_defs": PAYROLL_BREAKDOWN_DEFS,
            "period_month": (
                (month_label(result.start_date).rsplit(" ", 1)[0], str(result.start_date.year))
                if result.start_date.month == result.end_date.month
                and result.start_date.year == result.end_date.year
                else None
            ),
        },
    )


@app.post("/reports/presets", dependencies=[Depends(csrf_protect)])
async def save_report_preset_route(
    request: Request,
    admin: AdminAccount = Depends(get_current_admin),
):
    form = await request.form()
    name = str(form.get("preset_name", "")).strip()
    config = preset_config_from_params(form)
    _enforce_report_scope(ReportSelection.from_config(config), admin)
    try:
        with SessionLocal() as db:
            preset = save_preset(db, name, config, admin.username)
            db.commit()
            preset_id = preset.id
    except ValueError:
        return RedirectResponse("/reports?error=preset", status_code=303)
    return RedirectResponse(f"/reports?saved=1&preset_id={preset_id}", status_code=303)


@app.post("/reports/presets/{preset_id}/delete", dependencies=[Depends(csrf_protect)])
def delete_report_preset_route(
    preset_id: int,
    admin: AdminAccount = Depends(get_current_admin),
):
    with SessionLocal() as db:
        preset = load_preset(db, preset_id)
        if preset is not None:
            _enforce_report_scope(ReportSelection.from_config(preset_config(preset)), admin)
            db.delete(preset)
            db.commit()
    return RedirectResponse("/reports?deleted=1", status_code=303)


def _custom_report_requested(params) -> bool:
    return bool(params.get("report_type") or params.get("fields"))


_PAYROLL_REPORT_FIELD_KEYS = {key for key, _label in PAYROLL_FIELD_DEFS}
# National ID / passport numbers are personal identifiers outside the approved
# assistant scope, so attendance reports keep names and codes only.
_ASSISTANT_FORBIDDEN_FIELDS = _PAYROLL_REPORT_FIELD_KEYS | {"emp_id_proof"}
_ATTENDANCE_SAFE_FIELDS = {
    key for key, _label in EMPLOYEE_FIELD_DEFS + ATTENDANCE_FIELD_DEFS
} - _ASSISTANT_FORBIDDEN_FIELDS

# Report types each role may run, per the owner's approved split: the finance
# role owns payroll, advances and sanctions/rewards with read-only attendance;
# leave reports and the comprehensive export (which embeds the leave sheet)
# stay with the system administrator.
_REPORT_TYPES_BY_ROLE = {
    ROLE_ASSISTANT: {REPORT_TYPE_ATTENDANCE},
    ROLE_FINANCE: {
        REPORT_TYPE_PAYROLL,
        REPORT_TYPE_ATTENDANCE,
        REPORT_TYPE_ADVANCES,
        REPORT_TYPE_STAFF_ACTIONS,
        REPORT_TYPE_LEAVE,
    },
}


def _assistant_default_fields(params, selection: ReportSelection, admin: AdminAccount) -> None:
    """An assistant request with no explicit field choice gets the attendance-safe
    default set; an explicit payroll field choice stays visible so the scope check
    refuses it instead of silently trimming."""
    if admin.role != ROLE_ASSISTANT:
        return
    explicit = params.getlist("fields") if hasattr(params, "getlist") else params.get("fields")
    if not explicit:
        selection.fields = set(_ATTENDANCE_SAFE_FIELDS)


def _enforce_report_scope(selection: ReportSelection, admin: AdminAccount) -> None:
    """Each role runs only the report types its approved scope covers; the
    assistant's selection must not smuggle payroll or national-ID fields."""
    allowed = _REPORT_TYPES_BY_ROLE.get(admin.role)
    if allowed is None:
        return
    if selection.report_type not in allowed:
        raise HTTPException(status_code=403, detail="هذا الجزء غير متاح لصلاحية حسابك.")
    if admin.role == ROLE_ASSISTANT and (selection.fields & _ASSISTANT_FORBIDDEN_FIELDS):
        raise HTTPException(status_code=403, detail="هذا الجزء غير متاح لصلاحية حسابك.")


@app.get("/reports/export.xlsx")
def export_hr_reports(
    request: Request,
    start_date: date,
    end_date: date,
    employee_id: int | None = None,
    admin: AdminAccount = Depends(get_current_admin),
):
    params = request.query_params
    if admin.role != "system_admin" and not _custom_report_requested(params):
        # The all-modules workbook includes payroll and leave sheets.
        raise HTTPException(status_code=403, detail="هذا الجزء غير متاح لصلاحية حسابك.")
    try:
        with SessionLocal() as db:
            if _custom_report_requested(params):
                selection = selection_from_params(params)
                _assistant_default_fields(params, selection, admin)
                _enforce_report_scope(selection, admin)
                if selection.report_type == REPORT_TYPE_COMPREHENSIVE:
                    content = build_hr_workbook(db, start_date, end_date, employee_id=selection.employee_id, language=request.state.language)
                else:
                    content = build_custom_workbook(db, selection, language=request.state.language)
            else:
                content = build_hr_workbook(db, start_date, end_date, employee_id=employee_id, language=request.state.language)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="تعذر إنشاء التقرير بسبب خطأ في قاعدة البيانات.")
    _save_report_copy(content, "xlsx", start_date, end_date, employee_id)
    filename = f"MY_HR_Report_{start_date.isoformat()}_{end_date.isoformat()}.xlsx"
    from urllib.parse import quote
    return Response(
        content=content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )



@app.get("/reports/export.pdf")
def export_hr_reports_pdf(
    request: Request,
    start_date: date,
    end_date: date,
    employee_id: int | None = None,
    admin: AdminAccount = Depends(get_current_admin),
):
    params = request.query_params
    if admin.role != "system_admin" and not _custom_report_requested(params):
        # The all-modules PDF includes payroll and leave sections.
        raise HTTPException(status_code=403, detail="هذا الجزء غير متاح لصلاحية حسابك.")
    try:
        with SessionLocal() as db:
            if _custom_report_requested(params):
                selection = selection_from_params(params)
                _assistant_default_fields(params, selection, admin)
                _enforce_report_scope(selection, admin)
                if selection.report_type == REPORT_TYPE_COMPREHENSIVE:
                    content = build_hr_pdf(db, start_date, end_date, employee_id=selection.employee_id, language=request.state.language)
                else:
                    content = build_custom_pdf(db, selection, language=request.state.language)
            else:
                content = build_hr_pdf(db, start_date, end_date, employee_id=employee_id, language=request.state.language)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="تعذر إنشاء التقرير بسبب خطأ في قاعدة البيانات.")
    _save_report_copy(content, "pdf", start_date, end_date, employee_id)
    filename = f"MY_HR_Report_{start_date.isoformat()}_{end_date.isoformat()}.pdf"
    from urllib.parse import quote
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )

def _monthly_allowed_types(admin: AdminAccount) -> set[str] | None:
    if admin.role == ROLE_ASSISTANT:
        return {REPORT_TYPE_ATTENDANCE, REPORT_TYPE_LEAVE}
    return _REPORT_TYPES_BY_ROLE.get(admin.role)


@app.get("/reports/monthly.pdf")
def reports_monthly_pdf(
    request: Request,
    month: str = "",
    opt: list[int] = Query(default=[]),
    admin: AdminAccount = Depends(get_current_admin),
):
    """Approved monthly report: 11 checkboxes + explicit month, PDF only.

    Role scope matches the existing custom report: each option is allowed only
    when its underlying report type is in the role's approved set, and the
    assistant never receives payroll or national-ID content. Nothing is
    scheduled or sent automatically.
    """
    try:
        month_start = date.fromisoformat((month or "").strip() + "-01")
    except ValueError:
        raise HTTPException(status_code=400, detail="اختر شهرًا صالحًا للتقرير الشهري.")
    requested = set(opt)
    if not requested or requested - MONTHLY_ALL_OPTIONS:
        raise HTTPException(status_code=400, detail="خيارات التقرير الشهري غير صالحة.")
    allowed_types = _monthly_allowed_types(admin)
    allowed_options = monthly_options_for_types(allowed_types)
    rejected = requested - allowed_options
    if rejected:
        # Never silently drop options: a partial report with unannounced
        # omissions misleads. Name what the role cannot see and refuse.
        names = "، ".join(sorted(MONTHLY_OPTION_LABELS[o] for o in rejected if o in MONTHLY_OPTION_LABELS))
        raise HTTPException(status_code=403, detail=f"خيارات غير متاحة لصلاحية حسابك: {names}")
    options = requested
    data_options = options - {10, 11}
    if not data_options:
        raise HTTPException(status_code=403, detail="هذا الجزء غير متاح لصلاحية حسابك.")
    try:
        with SessionLocal() as db:
            content = build_monthly_pdf(
                db, month_start, options,
                language=request.state.language,
                aggregate_leave=(admin.role == ROLE_ASSISTANT),
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except SQLAlchemyError:
        raise HTTPException(status_code=503, detail="تعذر إنشاء التقرير بسبب خطأ في قاعدة البيانات.")
    month_end = date(month_start.year + (month_start.month == 12), month_start.month % 12 + 1, 1) - timedelta(days=1)
    _save_report_copy(content, "pdf", month_start, month_end, kind="monthly")
    filename = f"MY_HR_Monthly_{month_start.strftime('%Y-%m')}.pdf"
    from urllib.parse import quote
    return Response(
        content=content,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


def _telegram_context(request: Request) -> dict:
    with SessionLocal() as db:
        config = telegram_get_config(db)
    bind_link = None
    bind_expires = None
    expiry = config["bind_nonce_expires_at"]
    if (
        config["bot_username"]
        and config["bind_nonce"]
        and not config["bind_nonce_used"]
        and expiry is not None
        and expiry > datetime.now(timezone.utc)
    ):
        bind_link = telegram_bind_deep_link(config["bot_username"], config["bind_nonce"])
        bind_expires = expiry.astimezone().strftime("%Y-%m-%d %H:%M")
    return {
        "tg_configured": bool(config["bot_token"]),
        "tg_enabled": config["enabled"],
        "tg_chat_id": config["chat_id"],
        "tg_language": config["language"],
        "tg_bot_username": config["bot_username"],
        "tg_bind_link": bind_link,
        "tg_bind_expires": bind_expires,
        "tg_status": request.query_params.get("tg"),
    }


@app.get("/settings/password", response_class=HTMLResponse)
def password_page(request: Request, admin: AdminAccount = Depends(get_current_admin)):
    return templates.TemplateResponse(
        request=request,
        name="password.html",
        context={
            "error": None,
            "success": False,
            "forced": request.query_params.get("forced") == "1" or admin.must_change_password,
            "csrf_token": get_csrf_token(request),
            **_telegram_context(request),
        },
    )


def _revalidate_actor(db, admin: AdminAccount) -> AdminAccount | None:
    """Re-read the acting system administrator INSIDE the credential write
    lock. The session was authenticated before the lock was taken, so a
    restore (or another admin) may have changed the account in between; only
    a still-active system administrator may proceed."""
    actor = db.scalar(select(AdminAccount).where(AdminAccount.username == admin.username))
    if actor is None or not actor.active or actor.role != ROLE_SYSTEM_ADMIN:
        return None
    return actor


def _sensitive_reauth(admin: AdminAccount, current_password: str) -> str:
    """Gate for sensitive in-session actions: the administrator's own current
    password, with its own failure throttle so a hijacked unlocked session
    cannot guess without limit. Returns "ok", "failed" or "locked"."""
    with SessionLocal() as db:
        if reauth_lockout_remaining(db) is not None:
            return "locked"
    if current_password and verify_password(current_password, admin.password_hash):
        with SessionLocal() as db:
            acquire_credential_write_lock(db)
            clear_reauth_attempts(db, actor=admin.username)
        return "ok"
    with SessionLocal() as db:
        acquire_credential_write_lock(db)
        record_failed_reauth(db, actor=admin.username)
    return "failed"


@app.post("/settings/telegram", dependencies=[Depends(csrf_protect)])
def telegram_settings_save(
    request: Request,
    bot_token: str = Form(""),
    chat_id: str = Form(""),
    language: str = Form("ar"),
    enabled: str = Form(""),
    current_password: str = Form(""),
    admin: AdminAccount = Depends(require_system_admin),
):
    reauth = _sensitive_reauth(admin, current_password)
    if reauth != "ok":
        return RedirectResponse(f"/settings/password?tg={'auth_locked' if reauth == 'locked' else 'auth'}", status_code=303)
    with SessionLocal.begin() as db:
        # The mutation itself must hold the credential write lock (reauth's
        # lock ends with its own transaction) — otherwise a mid-restore
        # telegram config write can land in the half-restored window.
        acquire_credential_write_lock(db, expected_epoch=getattr(request.state, "restore_epoch", None))
        current = telegram_get_config(db)
        token = bot_token.strip() or current["bot_token"]
        token_changed = bool(bot_token.strip()) and token != current["bot_token"]
        bot_username = None
        if bot_token.strip():
            bot_username = verify_telegram_token(token)
            if bot_username is None:
                return RedirectResponse("/settings/password?tg=bad_token", status_code=303)
        telegram_save_config(
            db,
            bot_token=token,
            chat_id=("" if token_changed else (chat_id.strip() or current["chat_id"])),
            enabled=enabled == "on",
            language=language,
            actor=admin.username,
            bot_username=bot_username,
            clear_binding=token_changed,
        )
        db.add(AuditLog(
            actor=admin.username,
            action="telegram_alerts_configured",
            entity_type="system_setting",
            entity_id="telegram_alerts",
            old_value=None,
            new_value=f"enabled={enabled == 'on'}, chat_id={'set' if (chat_id.strip() or current['chat_id']) else 'unset'}",
            reason=None,
        ))
    return RedirectResponse("/settings/password?tg=saved", status_code=303)


@app.post("/settings/telegram/bind-link", dependencies=[Depends(csrf_protect)])
def telegram_bind_link(request: Request, current_password: str = Form(""), admin: AdminAccount = Depends(require_system_admin)):
    reauth = _sensitive_reauth(admin, current_password)
    if reauth != "ok":
        return RedirectResponse(f"/settings/password?tg={'auth_locked' if reauth == 'locked' else 'auth'}", status_code=303)
    with SessionLocal.begin() as db:
        acquire_credential_write_lock(db, expected_epoch=getattr(request.state, "restore_epoch", None))
        nonce = generate_telegram_bind_nonce(db, actor=admin.username)
        if nonce is None:
            return RedirectResponse("/settings/password?tg=need_token", status_code=303)
        db.add(AuditLog(
            actor=admin.username,
            action="telegram_bind_link_created",
            entity_type="system_setting",
            entity_id="telegram_alerts",
            old_value=None,
            new_value="single-use binding nonce issued",
            reason=None,
        ))
    return RedirectResponse("/settings/password?tg=link_created", status_code=303)


@app.post("/settings/telegram/bind-confirm", dependencies=[Depends(csrf_protect)])
def telegram_bind_confirm(request: Request, current_password: str = Form(""), admin: AdminAccount = Depends(require_system_admin)):
    reauth = _sensitive_reauth(admin, current_password)
    if reauth != "ok":
        return RedirectResponse(f"/settings/password?tg={'auth_locked' if reauth == 'locked' else 'auth'}", status_code=303)
    with SessionLocal.begin() as db:
        acquire_credential_write_lock(db, expected_epoch=getattr(request.state, "restore_epoch", None))
        status = bind_telegram_chat_from_nonce(db, actor=admin.username)
        if status == "bound":
            db.add(AuditLog(
                actor=admin.username,
                action="telegram_alerts_chat_bound",
                entity_type="system_setting",
                entity_id="telegram_alerts",
                old_value=None,
                new_value="private chat bound via single-use nonce",
                reason=None,
            ))
    if status == "no_token":
        status = "need_token"
    elif status != "bound":
        status = f"bind_{status}"
    return RedirectResponse(f"/settings/password?tg={status}", status_code=303)


@app.post("/settings/telegram/test", dependencies=[Depends(csrf_protect)])
def telegram_test_message(request: Request, current_password: str = Form(""), admin: AdminAccount = Depends(require_system_admin)):
    reauth = _sensitive_reauth(admin, current_password)
    if reauth != "ok":
        return RedirectResponse(f"/settings/password?tg={'auth_locked' if reauth == 'locked' else 'auth'}", status_code=303)
    with SessionLocal() as db:
        sent = send_telegram_test_message(db)
    return RedirectResponse(f"/settings/password?tg={'test_ok' if sent else 'test_failed'}", status_code=303)


@app.post("/settings/password", dependencies=[Depends(csrf_protect)])
def change_password_route(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    admin: AdminAccount = Depends(get_current_admin),
):
    def render(error: str | None, success: bool = False, status_code: int = 200):
        return templates.TemplateResponse(
            request=request,
            name="password.html",
            context={
                "error": error,
                "success": success,
                "forced": admin.must_change_password and not success,
                "csrf_token": get_csrf_token(request),
            },
            status_code=status_code,
        )

    if new_password != confirm_password:
        return render("mismatch", status_code=400)
    with SessionLocal.begin() as db:
        acquire_credential_write_lock(db, expected_epoch=getattr(request.state, "restore_epoch", None))
        db_admin = db.get(AdminAccount, admin.id)
        if db_admin is None or not verify_password(current_password, db_admin.password_hash):
            return render("wrong_current", status_code=400)
        if verify_password(new_password, db_admin.password_hash):
            return render("same", status_code=400)
        try:
            change_admin_password(db, db_admin, new_password)
        except ValueError:
            return render("too_short", status_code=400)
        db_admin.must_change_password = False
        db.add(AuditLog(
            actor=admin.username,
            action="admin_password_changed",
            entity_type="admin_account",
            entity_id="admin",
        ))
        new_session_version = db_admin.session_version
    # The rotation invalidates every other session; keep this one signed in.
    request.session["admin_session_version"] = new_session_version
    with SessionLocal.begin() as db:
        acquire_credential_write_lock(db, expected_epoch=getattr(request.state, "restore_epoch", None))
        db.execute(
            delete(AdminSession).where(
                AdminSession.admin_account_id == admin.id,
                AdminSession.id != (request.session.get("admin_session_id") or ""),
            )
        )
    return render(None, success=True)


@app.get("/settings/accounts", response_class=HTMLResponse)
def accounts_page(request: Request, admin: AdminAccount = Depends(require_system_admin)):
    return _accounts_page_response(request)


@app.post("/settings/accounts", dependencies=[Depends(csrf_protect)])
def create_account(
    request: Request,
    display_name: str = Form(...),
    username: str = Form(...),
    role: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    reason: str = Form(...),
    admin: AdminAccount = Depends(require_system_admin),
):
    display_name = display_name.strip()
    username = username.strip()
    reason = reason.strip()
    if not display_name or not username or not reason:
        return RedirectResponse("/settings/accounts?error=missing", status_code=303)
    # Exactly one system administrator exists (the HR manager); new accounts
    # are finance or assistant-manager only.
    if role not in (ROLE_FINANCE, ROLE_ASSISTANT):
        return RedirectResponse("/settings/accounts?error=role", status_code=303)
    if password != confirm_password:
        return RedirectResponse("/settings/accounts?error=mismatch", status_code=303)
    try:
        with SessionLocal.begin() as db:
            acquire_credential_write_lock(db, expected_epoch=getattr(request.state, "restore_epoch", None))
            existing = db.scalar(select(AdminAccount).where(AdminAccount.username == username))
            if existing is not None:
                return RedirectResponse("/settings/accounts?error=exists", status_code=303)
            account = AdminAccount(
                username=username,
                password_hash=hash_password(password),
                display_name=display_name,
                role=role,
                active=True,
            )
            db.add(account)
            db.flush()
            db.add(AuditLog(
                actor=admin.username,
                action="account_created",
                entity_type="admin_account",
                entity_id=str(account.id),
                old_value=None,
                new_value=f"{display_name} ({username}) - {role}",
                reason=reason,
            ))
    except ValueError:
        return RedirectResponse("/settings/accounts?error=weak", status_code=303)
    return RedirectResponse("/settings/accounts?saved=created", status_code=303)


@app.post("/settings/accounts/{account_id}/deactivate", dependencies=[Depends(csrf_protect)])
def deactivate_account(
    request: Request,
    account_id: int,
    reason: str = Form(...),
    target_username: str = Form(...),
    admin: AdminAccount = Depends(require_system_admin),
):
    reason = reason.strip()
    if not reason:
        return RedirectResponse("/settings/accounts?error=missing", status_code=303)
    with SessionLocal.begin() as db:
        acquire_credential_write_lock(db, expected_epoch=getattr(request.state, "restore_epoch", None))
        actor = _revalidate_actor(db, admin)
        if actor is None:
            return RedirectResponse("/settings/accounts?error=actor", status_code=303)
        target = db.get(AdminAccount, account_id)
        if target is None:
            return RedirectResponse("/settings/accounts?error=exists", status_code=303)
        if target.username != target_username:
            # The accounts page was rendered against a different database
            # state (a restore finalized meanwhile): this numeric id now
            # names another person, so refuse instead of mutating it.
            return RedirectResponse("/settings/accounts?error=stale", status_code=303)
        if target.id == actor.id:
            return RedirectResponse("/settings/accounts?error=self", status_code=303)
        if target.role == ROLE_SYSTEM_ADMIN and target.active:
            active_admins = db.scalar(
                select(func.count(AdminAccount.id)).where(
                    AdminAccount.role == ROLE_SYSTEM_ADMIN,
                    AdminAccount.active.is_(True),
                )
            ) or 0
            if active_admins <= 1:
                return RedirectResponse("/settings/accounts?error=last_admin", status_code=303)
        target.active = False
        target.session_version += 1
        db.execute(delete(AdminSession).where(AdminSession.admin_account_id == target.id))
        db.add(AuditLog(
            actor=admin.username,
            action="account_deactivated",
            entity_type="admin_account",
            entity_id=str(target.id),
            old_value=f"{target.display_name or target.username} - active",
            new_value="inactive",
            reason=reason,
        ))
    return RedirectResponse("/settings/accounts?saved=deactivated", status_code=303)


@app.post("/settings/accounts/{account_id}/activate", dependencies=[Depends(csrf_protect)])
def activate_account(
    request: Request,
    account_id: int,
    reason: str = Form(...),
    target_username: str = Form(...),
    admin: AdminAccount = Depends(require_system_admin),
):
    reason = reason.strip()
    if not reason:
        return RedirectResponse("/settings/accounts?error=missing", status_code=303)
    with SessionLocal.begin() as db:
        acquire_credential_write_lock(db, expected_epoch=getattr(request.state, "restore_epoch", None))
        actor = _revalidate_actor(db, admin)
        if actor is None:
            return RedirectResponse("/settings/accounts?error=actor", status_code=303)
        target = db.get(AdminAccount, account_id)
        if target is None:
            return RedirectResponse("/settings/accounts?error=exists", status_code=303)
        if target.username != target_username:
            return RedirectResponse("/settings/accounts?error=stale", status_code=303)
        if target.pending_credential_reset:
            # This account returned from an old archive: its archive-time
            # password hash is still on the row, so plain re-activation would
            # let a stolen old password sign in. It only comes back through
            # the audited reset flow below, which issues a fresh password and
            # destroys the old hash (owner-approved 2026-11-23).
            return RedirectResponse("/settings/accounts?error=reset_required", status_code=303)
        target.active = True
        db.add(AuditLog(
            actor=admin.username,
            action="account_activated",
            entity_type="admin_account",
            entity_id=str(target.id),
            old_value=f"{target.display_name or target.username} - inactive",
            new_value="active",
            reason=reason,
        ))
    return RedirectResponse("/settings/accounts?saved=activated", status_code=303)


_RESET_PASSWORD_ALPHABET = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789"


def _generate_temporary_password() -> str:
    """Readable one-time password, three groups of five unambiguous characters."""
    return "-".join(
        "".join(secrets.choice(_RESET_PASSWORD_ALPHABET) for _ in range(5))
        for _ in range(3)
    )


def _accounts_page_response(request: Request, reset_reveal: dict | None = None):
    with SessionLocal() as db:
        accounts = db.scalars(select(AdminAccount).order_by(AdminAccount.id)).all()
        rows = [
            {
                "id": account.id,
                "username": account.username,
                "display_name": account.display_name or account.username,
                "role": account.role,
                "role_label": ROLE_LABELS.get(account.role, account.role),
                "active": account.active,
                "pending_credential_reset": account.pending_credential_reset,
                "created_at": account.created_at,
            }
            for account in accounts
        ]
    with SessionLocal() as db:
        unused_codes = db.scalar(
            select(func.count())
            .select_from(RecoveryCode)
            .where(RecoveryCode.used_at.is_(None))
        ) or 0
        session_rows = db.scalars(
            select(AdminSession).order_by(AdminSession.created_at.desc())
        ).all()
        account_names = {row["id"]: row["display_name"] for row in rows}
        current_session_id = request.session.get("admin_session_id")
        sessions = [
            {
                "id": row.id,
                "account_name": account_names.get(row.admin_account_id, "؟"),
                "created_at": row.created_at,
                "last_seen_at": row.last_seen_at,
                "user_agent": (row.user_agent or "")[:80],
                "ip_address": row.ip_address or "",
                "is_current": row.id == current_session_id,
            }
            for row in session_rows
        ]
        regen_flag = db.get(SystemSetting, _RECOVERY_REGEN_SETTING_KEY)
        regen_required = bool(regen_flag is not None and regen_flag.value)
        regen_reason = (
            "legacy" if regen_flag is not None and regen_flag.value == "legacy_codes" else "restore"
        )
    return templates.TemplateResponse(
        request=request,
        name="accounts.html",
        context={
            "accounts": rows,
            "csrf_token": get_csrf_token(request),
            "error": request.query_params.get("error"),
            "saved": request.query_params.get("saved"),
            "reset_reveal": reset_reveal,
            "unused_recovery_codes": unused_codes,
            "sessions": sessions,
            "regen_required": regen_required,
            "regen_reason": regen_reason,
        },
    )


@app.post("/settings/accounts/sessions/{session_id}/end", dependencies=[Depends(csrf_protect)])
def end_admin_session(request: Request, session_id: str, admin: AdminAccount = Depends(require_system_admin)):
    """Terminate one open session; its cookie dies even if still validly signed."""
    with SessionLocal.begin() as db:
        acquire_credential_write_lock(db, expected_epoch=getattr(request.state, "restore_epoch", None))
        row = db.get(AdminSession, session_id)
        if row is not None:
            owner = db.get(AdminAccount, row.admin_account_id)
            db.add(AuditLog(
                actor=admin.username,
                action="session_ended",
                entity_type="admin_account",
                entity_id=str(row.admin_account_id),
                old_value=None,
                new_value=(owner.display_name or owner.username) if owner else "؟",
                reason=None,
            ))
            db.delete(row)
    return RedirectResponse("/settings/accounts?saved=1", status_code=303)


@app.post("/settings/accounts/{account_id}/reset-password", dependencies=[Depends(csrf_protect)])
def reset_account_password(
    request: Request,
    account_id: int,
    reason: str = Form(...),
    current_password: str = Form(...),
    target_username: str = Form(...),
    admin: AdminAccount = Depends(require_system_admin),
):
    """The system administrator issues a new one-time password for one account;
    it is shown once, never stored readable, and old sessions are closed."""
    reason = reason.strip()
    if not reason:
        return RedirectResponse("/settings/accounts?error=missing", status_code=303)
    # Reauthentication: whoever holds this session must prove they know the
    # administrator's own password before any account password is reset, so an
    # unlocked session alone cannot take over an account.
    reauth = _sensitive_reauth(admin, current_password)
    if reauth != "ok":
        return RedirectResponse(f"/settings/accounts?error={'auth_locked' if reauth == 'locked' else 'auth'}", status_code=303)
    temporary_password = _generate_temporary_password()
    with SessionLocal.begin() as db:
        acquire_credential_write_lock(db, expected_epoch=getattr(request.state, "restore_epoch", None))
        actor = _revalidate_actor(db, admin)
        if actor is None:
            return RedirectResponse("/settings/accounts?error=actor", status_code=303)
        target = db.get(AdminAccount, account_id)
        if target is None:
            return RedirectResponse("/settings/accounts?error=exists", status_code=303)
        if target.username != target_username:
            return RedirectResponse("/settings/accounts?error=stale", status_code=303)
        target.password_hash = hash_password(temporary_password)
        target.session_version += 1
        db.execute(delete(AdminSession).where(AdminSession.admin_account_id == target.id))
        target.must_change_password = True
        reactivated = False
        if target.pending_credential_reset:
            # Restore-locked account: the owner-approved re-enable is this
            # reset. The old archive hash is already destroyed above, so the
            # account can safely return to service with the fresh one-time
            # password; its owner still sets a personal password at first
            # sign-in (must_change_password).
            target.pending_credential_reset = False
            if not target.active:
                target.active = True
                reactivated = True
        db.add(AuditLog(
            actor=admin.username,
            action="account_password_reset",
            entity_type="admin_account",
            entity_id=str(target.id),
            old_value=None,
            new_value=f"{target.display_name or target.username}",
            reason=reason,
        ))
        if reactivated:
            db.add(AuditLog(
                actor=admin.username,
                action="account_reenabled_with_reset",
                entity_type="admin_account",
                entity_id=str(target.id),
                old_value="inactive, pending credential reset",
                new_value=f"{target.display_name or target.username} - active with new password",
                reason=reason,
            ))
        reveal = {"name": target.display_name or target.username, "password": temporary_password, "reactivated": reactivated}
    # Rendered directly (never through a redirect) so the one-time password
    # does not appear in URLs, browser history or server logs. The secret is
    # also never cacheable: a later back/forward or reopen must not restore it.
    response = _accounts_page_response(request, reset_reveal=reveal)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


RECOVERY_CODE_COUNT = 10


_RECOVERY_PEPPER_SETTING_KEY = "recovery_lookup_pepper"
_RECOVERY_REGEN_SETTING_KEY = "post_restore_regen_required"


def _recovery_lookup(db, normalized_code: str) -> str:
    """Keyed HMAC of a normalized recovery code: an index that finds the one
    candidate row without running Argon2 against every stored hash. The key is
    a random pepper stored in the database (created on first generation), so
    changing SESSION_SECRET never invalidates existing codes, and a backup
    restore rolls the pepper back together with the codes it indexes."""
    setting = db.get(SystemSetting, _RECOVERY_PEPPER_SETTING_KEY)
    if setting is None or not setting.value:
        setting = SystemSetting(
            key=_RECOVERY_PEPPER_SETTING_KEY,
            value=secrets.token_hex(32),
            updated_by="system",
        )
        db.add(setting)
        db.flush()
    return hmac.new(
        setting.value.encode("utf-8"),
        normalized_code.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


@functools.lru_cache(maxsize=1)
def _dummy_recovery_hash() -> str:
    """One constant Argon2 hash verified on every failed recovery attempt so
    unknown users and wrong codes cost the same work (no existence oracle)."""
    return hash_password("recovery-timing-equalizer")


def _generate_recovery_codes() -> list[str]:
    """Ten readable one-use codes, four groups of four unambiguous characters."""
    return [
        "-".join(
            "".join(secrets.choice(_RESET_PASSWORD_ALPHABET) for _ in range(4))
            for _ in range(4)
        )
        for _ in range(RECOVERY_CODE_COUNT)
    ]


@app.post("/settings/accounts/recovery-codes", dependencies=[Depends(csrf_protect)])
def generate_recovery_codes(
    request: Request,
    current_password: str = Form(...),
    admin: AdminAccount = Depends(require_system_admin),
):
    """Regenerate the system administrator's one-use recovery codes. Requires
    the administrator's own password; all previous codes die immediately."""
    reauth = _sensitive_reauth(admin, current_password)
    if reauth != "ok":
        return RedirectResponse(f"/settings/accounts?error={'auth_locked' if reauth == 'locked' else 'auth'}", status_code=303)
    codes = _generate_recovery_codes()
    with SessionLocal.begin() as db:
        acquire_credential_write_lock(db, expected_epoch=getattr(request.state, "restore_epoch", None))
        db.query(RecoveryCode).filter(
            RecoveryCode.admin_account_id == admin.id
        ).delete(synchronize_session=False)
        for code in codes:
            db.add(RecoveryCode(admin_account_id=admin.id, code_hash=hash_password(code), code_lookup=_recovery_lookup(db, code)))
        regen_flag = db.get(SystemSetting, _RECOVERY_REGEN_SETTING_KEY)
        if regen_flag is not None:
            regen_flag.value = ""
            regen_flag.updated_by = admin.username
        db.add(AuditLog(
            actor=admin.username,
            action="recovery_codes_generated",
            entity_type="admin_account",
            entity_id=str(admin.id),
            old_value=None,
            new_value=f"{RECOVERY_CODE_COUNT} one-use codes",
            reason=None,
        ))
    # The codes exist in readable form only in this response; it must never
    # be cached, and the hashes alone are stored.
    response = templates.TemplateResponse(
        request=request,
        name="recovery_codes.html",
        context={"codes": codes, "csrf_token": get_csrf_token(request)},
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


_RECOVERY_GENERIC_ERROR = "بيانات الاستعادة غير صحيحة."
_RECOVERY_LOCKED_ERROR = "تم إيقاف الاستعادة مؤقتًا بعد كثرة المحاولات. حاول مرة أخرى بعد"


def _render_recover(request: Request, error: str | None, status_code: int = 200,
                    lockout_minutes: int | None = None):
    with SessionLocal() as db:
        remaining = recovery_lockout_remaining(db)
        backup_passphrase_required = _post_restore_passphrase_armed(db)
    if remaining is not None:
        error = _RECOVERY_LOCKED_ERROR
        lockout_minutes = max(1, math.ceil(remaining.total_seconds() / 60))
    return templates.TemplateResponse(
        request=request,
        name="recover.html",
        context={
            "error": error,
            "lockout_minutes": lockout_minutes,
            "backup_passphrase_required": backup_passphrase_required,
            "csrf_token": get_csrf_token(request),
        },
        status_code=status_code,
    )


@app.get("/login/recover", response_class=HTMLResponse)
def recover_page(request: Request):
    return _render_recover(request, None)


@app.post("/login/recover", dependencies=[Depends(csrf_protect)])
def recover_account(
    request: Request,
    username: str = Form(...),
    recovery_code: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    backup_passphrase: str = Form(""),
):
    """Offline forgotten-password recovery for the system administrator:
    username + one unused recovery code + a new password. Every failure —
    unknown user, wrong code, inactive account — returns the same generic
    error so the flow cannot enumerate accounts or codes."""
    with SessionLocal() as db:
        if recovery_lockout_remaining(db) is not None:
            return _render_recover(request, _RECOVERY_LOCKED_ERROR, status_code=429)

    if new_password != confirm_password or len(new_password) < _MIN_PASSWORD_LENGTH:
        return _render_recover(request, _RECOVERY_GENERIC_ERROR, status_code=400)

    with SessionLocal() as db:
        # Whole verify+reset runs under the write lock: a recovery code can
        # never be consumed against half-restored credential state.
        acquire_credential_write_lock(db)
        passphrase_armed = _post_restore_passphrase_armed(db)
        if passphrase_armed:
            # Guesses share the sensitive-action throttle, as on the login gate.
            if reauth_lockout_remaining(db) is not None:
                return _render_recover(request, "أُوقفت هذه العملية مؤقتًا بعد كثرة المحاولات. حاول لاحقًا.", status_code=429)
            from app.backup_crypto import unwrap_data_key_for_restore
            try:
                unwrap_data_key_for_restore(db, backup_passphrase)
            except Exception:
                record_failed_reauth(db, actor="post_restore_recover")
                return _render_recover(request, "كلمة مرور النسخ الاحتياطية غير صحيحة.", status_code=401)
        account = db.scalar(
            select(AdminAccount).where(
                AdminAccount.username == username.strip(),
                AdminAccount.role == ROLE_SYSTEM_ADMIN,
                AdminAccount.active == True,  # noqa: E712
            )
        )
        # Codes are case-sensitive; only surrounding whitespace is ignored.
        normalized_code = recovery_code.strip()
        matched_id = None
        verified_once = False
        if account is not None:
            candidate = db.scalar(
                select(RecoveryCode).where(
                    RecoveryCode.admin_account_id == account.id,
                    RecoveryCode.used_at.is_(None),
                    RecoveryCode.code_lookup == _recovery_lookup(db, normalized_code),
                )
            )
            if candidate is not None:
                verified_once = True
                if verify_password(normalized_code, candidate.code_hash):
                    matched_id = candidate.id
        if matched_id is None and not verified_once:
            # Rows predating the lookup column still verify one by one. The
            # fallback deliberately scans EVERY unused legacy row regardless of
            # the submitted username (recovery codes only ever belong to the
            # system administrator), so a failed attempt costs the same number
            # of verifications whether or not the account exists — per-account
            # filtering would leak account existence through timing. Only a
            # match that also belongs to the submitted account can recover it.
            legacy = db.scalars(
                select(RecoveryCode).where(
                    RecoveryCode.used_at.is_(None),
                    RecoveryCode.code_lookup.is_(None),
                )
            ).all()
            for candidate in legacy:
                verified_once = True
                if verify_password(normalized_code, candidate.code_hash):
                    if account is not None and candidate.admin_account_id == account.id:
                        matched_id = candidate.id
        if matched_id is None and not verified_once:
            # Equalize failure work: with no legacy rows, unknown user, inactive
            # account and wrong code all pay exactly one Argon2 verification,
            # so response timing does not reveal whether the account exists.
            verify_password(normalized_code, _dummy_recovery_hash())

        if account is None or matched_id is None:
            record_failed_recovery(db, actor="recovery")
            return _render_recover(request, _RECOVERY_GENERIC_ERROR, status_code=401)

        # Consume the code atomically: only the first concurrent request wins.
        consumed = db.execute(
            update(RecoveryCode)
            .where(RecoveryCode.id == matched_id, RecoveryCode.used_at.is_(None))
            .values(used_at=datetime.now(timezone.utc))
        ).rowcount
        if consumed != 1:
            record_failed_recovery(db, actor="recovery")
            return _render_recover(request, _RECOVERY_GENERIC_ERROR, status_code=401)

        account.password_hash = hash_password(new_password)
        account.session_version += 1
        account.must_change_password = False
        db.execute(delete(AdminSession).where(AdminSession.admin_account_id == account.id))
        if passphrase_armed:
            clear_reauth_attempts(db, actor="post_restore_recover")
            db.merge(SystemSetting(key="post_restore_passphrase_required", value="", updated_by=account.username))
            db.add(AuditLog(
                actor=account.username,
                action="post_restore_passphrase_verified",
                entity_type="admin_account",
                entity_id=account.username,
                reason="Backup passphrase presented during post-restore account recovery.",
            ))
        db.add(AuditLog(
            actor=account.username,
            action="admin_password_recovered",
            entity_type="admin_account",
            entity_id=str(account.id),
            old_value=None,
            new_value="recovered with one-use code",
            reason=None,
        ))
        clear_recovery_attempts(db, actor="recovery")
        db.commit()
    return RedirectResponse("/login?recovered=1", status_code=303)


AUDIT_PAGE_SIZE = 50


@app.get("/settings/audit", response_class=HTMLResponse)
def audit_log_page(
    request: Request,
    q: str = "",
    entity_type: str = "",
    from_date: str = "",
    to_date: str = "",
    page: int = 1,
    admin: AdminAccount = Depends(require_system_admin),
):
    from app.audit_display import ACTION_LABELS, ENTITY_LABELS

    page = max(1, page)
    with SessionLocal() as db:
        query = select(AuditLog)
        if q.strip():
            pattern = f"%{q.strip()}%"
            query = query.where(
                (AuditLog.action.ilike(pattern))
                | (AuditLog.entity_id.ilike(pattern))
                | (AuditLog.reason.ilike(pattern))
            )
        if entity_type.strip():
            query = query.where(AuditLog.entity_type == entity_type.strip())
        parsed_from = _parse_date(from_date)
        if parsed_from is not None:
            query = query.where(AuditLog.created_at >= datetime.combine(parsed_from, datetime.min.time(), tzinfo=CAIRO_TZ))
        parsed_to = _parse_date(to_date)
        if parsed_to is not None:
            query = query.where(AuditLog.created_at < datetime.combine(parsed_to + timedelta(days=1), datetime.min.time(), tzinfo=CAIRO_TZ))
        total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
        pages = max(1, math.ceil(total / AUDIT_PAGE_SIZE))
        page = min(page, pages)
        entries = db.scalars(
            query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .offset((page - 1) * AUDIT_PAGE_SIZE)
            .limit(AUDIT_PAGE_SIZE)
        ).all()
        entity_options = sorted(ENTITY_LABELS.items(), key=lambda item: item[1])
        return templates.TemplateResponse(
            request=request,
            name="audit.html",
            context={
                "entries": entries,
                "total": total,
                "page": page,
                "pages": pages,
                "filters": {"q": q, "entity_type": entity_type, "from_date": from_date, "to_date": to_date},
                "entity_options": entity_options,
                "action_labels": ACTION_LABELS,
                "entity_labels": ENTITY_LABELS,
                "csrf_token": get_csrf_token(request),
            },
        )


@app.get("/settings/backup", response_class=HTMLResponse)
def backup_settings_page(
    request: Request,
    admin: AdminAccount = Depends(require_system_admin),
):
    with SessionLocal() as db:
        root = get_backup_root(db)
        runs = db.scalars(
            select(BackupRun).order_by(BackupRun.created_at.desc(), BackupRun.id.desc()).limit(50)
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="backup.html",
            context={
                "backup_root": str(root) if root else "",
                "runs": runs,
                "encryption_enabled": backup_has_passphrase(db),
                "csrf_token": get_csrf_token(request),
                "error": request.query_params.get("error"),
                "success": request.query_params.get("success"),
            },
        )


@app.post("/settings/backup/passphrase", dependencies=[Depends(csrf_protect)])
def update_backup_passphrase_route(
    new_passphrase: str = Form(...),
    confirm_passphrase: str = Form(...),
    current_passphrase: str = Form(""),
    admin: AdminAccount = Depends(require_system_admin),
):
    if new_passphrase != confirm_passphrase:
        return RedirectResponse("/settings/backup?error=passphrase_mismatch", status_code=303)
    try:
        with SessionLocal.begin() as db:
            set_backup_passphrase(
                db,
                new_passphrase,
                admin.username,
                current_passphrase=current_passphrase or None,
            )
            export_key_file(db)
    except WrongPassphraseError:
        return RedirectResponse("/settings/backup?error=passphrase_wrong", status_code=303)
    except ValueError as exc:
        if str(exc) == "passphrase_too_short":
            return RedirectResponse("/settings/backup?error=passphrase_short", status_code=303)
        return RedirectResponse("/settings/backup?error=passphrase_wrong", status_code=303)
    return RedirectResponse("/settings/backup?success=passphrase", status_code=303)


@app.post("/settings/backup/root", dependencies=[Depends(csrf_protect)])
def update_backup_root_route(
    backup_root: str = Form(...),
    admin: AdminAccount = Depends(require_system_admin),
):
    try:
        with SessionLocal.begin() as db:
            save_backup_root(db, backup_root, admin.username)
    except (ValueError, OSError):
        return RedirectResponse("/settings/backup?error=path", status_code=303)
    return RedirectResponse("/settings/backup?success=path", status_code=303)


@app.post("/settings/backup/create", dependencies=[Depends(csrf_protect)])
def create_backup_route(
    backup_type: str = Form("manual"),
    admin: AdminAccount = Depends(require_system_admin),
):
    if backup_type not in {"daily", "weekly", "quarterly", "manual"}:
        return RedirectResponse("/settings/backup?error=type", status_code=303)
    with SessionLocal.begin() as db:
        run = create_backup(db, backup_type, admin.username)
        run_id = run.id
        status = run.status
    if status != "success":
        return RedirectResponse(f"/settings/backup?error=backup&run={run_id}", status_code=303)
    return RedirectResponse(f"/settings/backup?success=backup&run={run_id}", status_code=303)


@app.post("/settings/backup/restore", dependencies=[Depends(csrf_protect)])
def restore_backup_route(
    backup_id: int = Form(...),
    confirm_text: str = Form(...),
    reason: str = Form(...),
    passphrase: str = Form(""),
    current_password: str = Form(""),
    db: Session = Depends(get_db),
    admin: AdminAccount = Depends(require_system_admin),
):
    # A restore is the most destructive in-app action, and it runs while the
    # administrator is signed in — so an unlocked session alone must not be
    # enough to trigger it. Same reauth gate as the other sensitive actions.
    reauth = _sensitive_reauth(admin, current_password)
    if reauth == "locked":
        return RedirectResponse("/settings/backup?error=auth_locked", status_code=303)
    if reauth != "ok":
        return RedirectResponse("/settings/backup?error=auth", status_code=303)
    if confirm_text.strip() != "RESTORE" or not reason.strip():
        return RedirectResponse("/settings/backup?error=confirm", status_code=303)
    run = db.get(BackupRun, backup_id)
    if run is None or run.status != "success" or not run.archive_path or not run.sha256:
        return RedirectResponse("/settings/backup?error=archive", status_code=303)
    archive_path = run.archive_path
    expected_sha256 = run.sha256
    db.close()
    try:
        restore_backup(
            archive_path,
            expected_sha256,
            actor=admin.username,
            reason=reason,
            passphrase=passphrase or None,
        )
    except (PassphraseRequiredError, WrongPassphraseError):
        return RedirectResponse("/settings/backup?error=passphrase_wrong", status_code=303)
    except Exception:
        return RedirectResponse("/settings/backup?error=restore", status_code=303)
    return RedirectResponse("/login?restored=1", status_code=303)


EMPLOYEES_PER_PAGE = 24
# Paginate the correction-request review list so the page never loads the full
# history while every request stays reachable.
DATA_CHANGE_REQUESTS_PER_PAGE = 20


@app.get("/employees", response_class=HTMLResponse)
def employees_page(
    request: Request,
    q: str = "",
    department_id: int = 0,
    branch_id: int = 0,
    status: str = "",
    page: int = 1,
    req_page: int = 1,
    admin: AdminAccount = Depends(require_employee_read),
):
    with SessionLocal() as db:
        departments = db.scalars(select(Department).where(Department.active.is_(True)).order_by(Department.name)).all()
        branches = db.scalars(select(Branch).where(Branch.active.is_(True)).order_by(Branch.name)).all()
        all_departments = db.scalars(select(Department).order_by(Department.name)).all()
        all_branches = db.scalars(select(Branch).order_by(Branch.name)).all()
        stmt = select(Employee)
        query = q.strip()
        if query:
            like = f"%{query}%"
            stmt = stmt.where(or_(
                Employee.full_name.ilike(like),
                Employee.code.ilike(like),
                Employee.national_id.ilike(like),
                Employee.phone.ilike(like),
            ))
        if department_id:
            stmt = stmt.where(Employee.department_id == department_id)
        if branch_id:
            stmt = stmt.where(Employee.branch_id == branch_id)
        if status in ("active", "archived"):
            stmt = stmt.where(Employee.status == status)
        total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
        per_page = EMPLOYEES_PER_PAGE
        pages = max(1, math.ceil(total / per_page))
        page = min(max(1, page), pages)
        employees = db.scalars(
            stmt.order_by(Employee.code).offset((page - 1) * per_page).limit(per_page)
        ).all()

        # Change history renders only inside the visible cards, so load it just
        # for the employees on this page instead of the entire table.
        page_employee_ids = [e.id for e in employees] or [-1]

        # Employee-card summary tiles (approved UI batch): leave balance,
        # current-month recorded attendance, latest net pay, service years.
        # Payroll data is computed only for roles already allowed to see it.
        today = date.today()
        month_start = today.replace(day=1)
        card_tiles: dict[int, dict] = {}
        attendance_counts = db.execute(
            select(
                AttendanceDailyRecord.employee_id,
                func.count(),
                func.sum(case((AttendanceDailyRecord.worked_minutes > 0, 1), else_=0)),
            )
            .where(
                AttendanceDailyRecord.employee_id.in_(page_employee_ids),
                AttendanceDailyRecord.work_date >= month_start,
                AttendanceDailyRecord.work_date <= today,
                AttendanceDailyRecord.status == "approved",
            )
            .group_by(AttendanceDailyRecord.employee_id)
        ).all()
        attendance_map = {emp_id: (total or 0, present or 0) for emp_id, total, present in attendance_counts}
        leave_totals: dict[int, Decimal] = {}
        leave_types = db.scalars(select(LeaveType).order_by(LeaveType.id)).all()
        if leave_types:
            balance_sums = db.execute(
                select(LeaveBalanceTransaction.employee_id, func.sum(LeaveBalanceTransaction.days_delta))
                .where(LeaveBalanceTransaction.employee_id.in_(page_employee_ids))
                .group_by(LeaveBalanceTransaction.employee_id)
            ).all()
            leave_totals = {emp_id: (total or Decimal("0")) for emp_id, total in balance_sums}
        can_payroll = admin.role in (ROLE_SYSTEM_ADMIN, ROLE_FINANCE)
        latest_payroll: dict[int, tuple[PayrollItem, PayrollRun]] = {}
        if can_payroll:
            payroll_rows = db.execute(
                select(PayrollItem, PayrollRun)
                .join(PayrollRun, PayrollRun.id == PayrollItem.payroll_run_id)
                .where(PayrollItem.employee_id.in_(page_employee_ids))
                .order_by(PayrollItem.employee_id, PayrollRun.period_start.desc(), PayrollRun.id.desc())
            ).all()
            for item, run in payroll_rows:
                latest_payroll.setdefault(item.employee_id, (item, run))
        for e in employees:
            total_days, present_days = attendance_map.get(e.id, (0, 0))
            service_years = None
            if e.hire_date:
                service_years = round((today - e.hire_date).days / 365.25, 1)
            latest = latest_payroll.get(e.id)
            card_tiles[e.id] = {
                "leave_balance": leave_totals.get(e.id, Decimal("0")),
                "att_present": present_days,
                "att_total": total_days,
                "net_pay": latest[0].net_pay if latest else None,
                "payslip_run_id": latest[1].id if latest else None,
                "payslip_item_id": latest[0].id if latest else None,
                "service_years": service_years,
            }
        employee_changes = db.scalars(
            select(EmployeeChange)
            .where(EmployeeChange.employee_id.in_(page_employee_ids))
            .order_by(EmployeeChange.effective_date.desc(), EmployeeChange.id.desc())
        ).all()
        data_change_total = db.scalar(select(func.count()).select_from(EmployeeDataChangeRequest)) or 0
        req_pages = max(1, math.ceil(data_change_total / DATA_CHANGE_REQUESTS_PER_PAGE))
        req_page = min(max(1, req_page), req_pages)
        data_change_requests = db.scalars(
            select(EmployeeDataChangeRequest)
            .order_by(EmployeeDataChangeRequest.recorded_at.desc())
            .offset((req_page - 1) * DATA_CHANGE_REQUESTS_PER_PAGE)
            .limit(DATA_CHANGE_REQUESTS_PER_PAGE)
        ).all()

        def page_url(target: int) -> str:
            params = {"q": query, "department_id": department_id, "branch_id": branch_id, "status": status, "page": target}
            if req_page > 1:
                params["req_page"] = req_page
            return "/employees?" + urlencode({k: v for k, v in params.items() if v not in ("", 0)})

        def req_page_url(target: int) -> str:
            params = {"q": query, "department_id": department_id, "branch_id": branch_id, "status": status, "req_page": target}
            if page > 1:
                params["page"] = page
            return "/employees?" + urlencode({k: v for k, v in params.items() if v not in ("", 0)})

        return templates.TemplateResponse(
            request=request, name="employees.html",
            context={"departments": departments, "branches": branches, "all_departments": all_departments, "all_branches": all_branches, "employee_changes": employee_changes, "card_tiles": card_tiles, "can_payroll": can_payroll, "data_change_requests": data_change_requests, "employees": employees, "csrf_token": get_csrf_token(request), "q": query, "filter_department_id": department_id, "filter_branch_id": branch_id, "filter_status": status, "filters_active": bool(query or department_id or branch_id or status), "page": page, "pages": pages, "total": total, "page_url": page_url, "req_page": req_page, "req_pages": req_pages, "req_page_url": req_page_url},
        )


@app.post("/employees", dependencies=[Depends(csrf_protect)])
def create_employee(
    request: Request,
    full_name: str = Form(...),
    national_id: str = Form(""),
    nationality: str = Form("egyptian"),
    passport_number: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    department_id: int = Form(...),
    branch_id: int | None = Form(None),
    position: str = Form(...),
    hire_date: date = Form(...),
    probation_start_date: date | None = Form(None),
    probation_end_date: date | None = Form(None),
    basic_salary: Decimal = Form(...),
    daily_hours: Decimal = Form(...),
    fingerprint_id: str = Form(""),
    photo: UploadFile | None = File(None),
    admin: AdminAccount = Depends(require_system_admin),
):
    if basic_salary < 0 or daily_hours <= 0 or daily_hours > 24:
        return HTMLResponse("بيانات الراتب أو ساعات العمل غير صحيحة.", status_code=400)
    if probation_start_date and probation_end_date and probation_end_date < probation_start_date:
        raise HTTPException(status_code=400, detail="نهاية فترة الاختبار لا يمكن أن تسبق بدايتها.")
    full_name = full_name.strip()
    position = position.strip()
    national_id = national_id.strip() or None
    fingerprint_id = fingerprint_id.strip() or None
    nationality = nationality.strip() or "egyptian"
    if nationality not in ("egyptian", "foreign"):
        raise HTTPException(status_code=400, detail="الجنسية يجب أن تكون مصري أو أجنبي.")
    passport_number = passport_number.strip() or None
    if nationality == "foreign" and not passport_number:
        raise HTTPException(status_code=400, detail="رقم الباسبور مطلوب للموظف الأجنبي.")
    if nationality == "egyptian":
        passport_number = None
        if national_id and not is_valid_egyptian_national_id(national_id):
            raise HTTPException(status_code=400, detail=NATIONAL_ID_ERROR)
    if not full_name or not position:
        raise HTTPException(status_code=400, detail="اسم الموظف والمسمى الوظيفي مطلوبان.")
    with SessionLocal.begin() as db:
        department = db.get(Department, department_id)
        if department is None or not department.active:
            raise HTTPException(status_code=400, detail="القسم المحدد غير متاح.")
        branch = db.get(Branch, branch_id) if branch_id is not None else None
        if branch_id is not None and (branch is None or not branch.active or branch.department_id != department_id):
            raise HTTPException(status_code=400, detail="الفرع المحدد غير تابع للقسم المختار أو غير متاح.")
        for field_name, value in (("national_id", national_id), ("fingerprint_id", fingerprint_id), ("passport_number", passport_number)):
            if value is not None and db.scalar(
                select(Employee.id).where(getattr(Employee, field_name) == value).limit(1)
            ) is not None:
                raise HTTPException(status_code=409, detail=f"القيمة المدخلة في {field_name} مستخدمة لموظف آخر.")
        photo_path = _store_employee_photo(photo)
        employee = Employee(
            code=allocate_employee_code(db), full_name=full_name,
            national_id=national_id, nationality=nationality, passport_number=passport_number,
            phone=phone.strip() or None,
            address=address.strip() or None, department_id=department_id,
            branch_id=branch_id, position=position, hire_date=hire_date,
            probation_start_date=probation_start_date, probation_end_date=probation_end_date,
            basic_salary=basic_salary, daily_hours=daily_hours,
            fingerprint_id=fingerprint_id, photo_path=photo_path, status="active",
        )
        db.add(employee)
        db.flush()
        db.add(EmployeeServicePeriod(employee_id=employee.id, start_date=hire_date, start_reason="Initial employment"))
        db.add(AuditLog(
            actor=admin.username, action="employee_created",
            entity_type="employee", entity_id=str(employee.id),
            new_value=f"code={employee.code}; full_name={employee.full_name}; department_id={department_id}; branch_id={branch_id}",
            reason="Initial employee record created",
        ))
    return RedirectResponse("/employees", status_code=303)


@app.post("/employees/{employee_id}/edit", dependencies=[Depends(csrf_protect)])
def edit_employee(
    employee_id: int,
    full_name: str = Form(...),
    national_id: str = Form(""),
    nationality: str = Form("egyptian"),
    passport_number: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    department_id: int = Form(...),
    branch_id: int | None = Form(None),
    position: str = Form(...),
    basic_salary: Decimal = Form(...),
    daily_hours: Decimal = Form(...),
    fingerprint_id: str = Form(""),
    probation_start_date: date | None = Form(None),
    probation_end_date: date | None = Form(None),
    effective_date: date = Form(...),
    reason: str = Form(...),
    admin: AdminAccount = Depends(require_system_admin),
):
    reason = reason.strip()
    full_name = full_name.strip()
    position = position.strip()
    if not reason or not full_name or not position:
        raise HTTPException(status_code=400, detail="الاسم والمسمى وسبب التعديل حقول مطلوبة.")
    if basic_salary < 0 or daily_hours <= 0 or daily_hours > 24:
        raise HTTPException(status_code=400, detail="بيانات الراتب أو ساعات العمل غير صحيحة.")
    if probation_start_date and probation_end_date and probation_end_date < probation_start_date:
        raise HTTPException(status_code=400, detail="نهاية فترة الاختبار لا يمكن أن تسبق بدايتها.")
    if effective_date > date.today():
        raise HTTPException(
            status_code=400,
            detail="تاريخ سريان تعديل بيانات الموظف لا يمكن أن يكون في المستقبل.",
        )

    with SessionLocal.begin() as db:
        employee = db.get(Employee, employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="الموظف غير موجود.")
        department = db.get(Department, department_id)
        if department is None or not department.active:
            raise HTTPException(status_code=400, detail="القسم المحدد غير متاح.")
        if branch_id is not None:
            branch = db.get(Branch, branch_id)
            if branch is None or not branch.active or branch.department_id != department_id:
                raise HTTPException(status_code=400, detail="الفرع المحدد غير تابع للقسم المختار أو غير متاح.")

        nationality = nationality.strip() or "egyptian"
        if nationality not in ("egyptian", "foreign"):
            raise HTTPException(status_code=400, detail="الجنسية يجب أن تكون مصري أو أجنبي.")
        passport_number = passport_number.strip() or None
        if nationality == "foreign" and not passport_number:
            raise HTTPException(status_code=400, detail="رقم الباسبور مطلوب للموظف الأجنبي.")
        if nationality == "egyptian":
            passport_number = None
        normalized_national_id = national_id.strip() or None
        # Validate only when the Egyptian ID is actually changing; stored
        # legacy values are never revalidated.
        if (
            nationality == "egyptian"
            and normalized_national_id is not None
            and normalized_national_id != employee.national_id
            and not is_valid_egyptian_national_id(normalized_national_id)
        ):
            raise HTTPException(status_code=400, detail=NATIONAL_ID_ERROR)
        normalized = {
            "full_name": full_name,
            "national_id": normalized_national_id,
            "nationality": nationality,
            "passport_number": passport_number,
            "phone": phone.strip() or None,
            "address": address.strip() or None,
            "department_id": department_id,
            "branch_id": branch_id,
            "position": position,
            "basic_salary": basic_salary,
            "daily_hours": daily_hours,
            "fingerprint_id": fingerprint_id.strip() or None,
            "probation_start_date": probation_start_date,
            "probation_end_date": probation_end_date,
        }
        for field_name in ("national_id", "fingerprint_id", "passport_number"):
            value = normalized[field_name]
            if value is None:
                continue
            duplicate = db.scalar(
                select(Employee.id).where(
                    getattr(Employee, field_name) == value,
                    Employee.id != employee.id,
                ).limit(1)
            )
            if duplicate is not None:
                raise HTTPException(status_code=409, detail=f"القيمة المدخلة في {field_name} مستخدمة لموظف آخر.")

        changes = []
        for field_name, new_value in normalized.items():
            old_value = getattr(employee, field_name)
            if old_value != new_value:
                changes.append((field_name, old_value, new_value))
        for field_name, old_value, new_value in changes:
            setattr(employee, field_name, new_value)
            db.add(EmployeeChange(
                employee_id=employee.id,
                field_name=field_name,
                old_value=str(old_value) if old_value is not None else None,
                new_value=str(new_value) if new_value is not None else None,
                effective_date=effective_date,
                reason=reason,
            ))
        if changes:
            db.add(AuditLog(
                actor=admin.username,
                action="employee_updated",
                entity_type="employee",
                entity_id=str(employee.id),
                old_value="; ".join(f"{name}={old}" for name, old, _ in changes),
                new_value="; ".join(f"{name}={new}" for name, _, new in changes),
                reason=reason,
            ))
    return RedirectResponse("/employees", status_code=303)





@app.post("/employees/{employee_id}/data-change-requests", dependencies=[Depends(csrf_protect)])
def create_employee_data_change_request(
    employee_id: int,
    field_name: str = Form(...),
    requested_value: str = Form(...),
    request_reason: str = Form(...),
    admin: AdminAccount = Depends(require_system_admin),
):
    requested_value = requested_value.strip()
    request_reason = request_reason.strip()
    if field_name not in EMPLOYEE_PHOTO_FIELDS:
        raise HTTPException(status_code=400, detail="نوع البيانات المطلوب تغييره غير مسموح.")
    if not requested_value or not request_reason:
        raise HTTPException(status_code=400, detail="القيمة المطلوبة وسبب الطلب مطلوبان.")
    with SessionLocal.begin() as db:
        employee = db.get(Employee, employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="الموظف غير موجود.")
        db.add(EmployeeDataChangeRequest(
            employee_id=employee_id, field_name=field_name,
            requested_value=requested_value, request_reason=request_reason,
            status="pending",
        ))
        db.add(AuditLog(
            actor=admin.username, action="employee_data_change_requested",
            entity_type="employee", entity_id=str(employee_id),
            new_value=f"{field_name}={requested_value}", reason=request_reason,
        ))
    return RedirectResponse("/employees", status_code=303)


@app.post("/employee-data-change-requests/{request_id}/review", dependencies=[Depends(csrf_protect)])
def review_employee_data_change_request(
    request_id: int,
    decision: str = Form(...),
    decision_reason: str = Form(...),
    effective_date: date | None = Form(None),
    admin: AdminAccount = Depends(require_system_admin),
):
    decision_reason = decision_reason.strip()
    if decision not in {"apply", "reject"} or not decision_reason:
        raise HTTPException(status_code=400, detail="قرار المراجعة وسببه مطلوبان.")
    with SessionLocal.begin() as db:
        change_request = db.get(EmployeeDataChangeRequest, request_id)
        if change_request is None:
            raise HTTPException(status_code=404, detail="طلب التعديل غير موجود.")
        if change_request.status != "pending":
            raise HTTPException(status_code=409, detail="تمت مراجعة هذا الطلب من قبل.")
        employee = db.get(Employee, change_request.employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="الموظف المرتبط بالطلب غير موجود.")
        old_value = None
        new_value = None
        if decision == "apply":
            if effective_date is None:
                raise HTTPException(status_code=400, detail="تاريخ سريان التعديل مطلوب عند الموافقة.")
            if change_request.field_name not in EMPLOYEE_PHOTO_FIELDS:
                raise HTTPException(status_code=400, detail="حقل التعديل غير مسموح.")
            old_value = getattr(employee, change_request.field_name)
            new_value = change_request.requested_value
            if change_request.field_name == "national_id":
                duplicate = db.scalar(select(Employee.id).where(
                    Employee.national_id == new_value, Employee.id != employee.id
                ).limit(1))
                if duplicate is not None:
                    raise HTTPException(status_code=409, detail="الرقم القومي مستخدم لموظف آخر.")
                if (
                    employee.nationality == "egyptian"
                    and new_value
                    and not is_valid_egyptian_national_id(new_value)
                ):
                    raise HTTPException(status_code=400, detail=NATIONAL_ID_ERROR)
            setattr(employee, change_request.field_name, new_value)
            db.add(EmployeeChange(
                employee_id=employee.id, field_name=change_request.field_name,
                old_value=str(old_value) if old_value is not None else None,
                new_value=new_value, effective_date=effective_date,
                reason=f"Approved request #{request_id}: {decision_reason}",
            ))
            change_request.status = "applied"
            change_request.effective_date = effective_date
        else:
            change_request.status = "rejected"
        change_request.decision_reason = decision_reason
        change_request.reviewed_at = datetime.now(timezone.utc)
        change_request.reviewed_by = admin.username
        db.add(AuditLog(
            actor=admin.username, action=f"employee_data_change_{change_request.status}",
            entity_type="employee_data_change_request", entity_id=str(request_id),
            old_value=str(old_value) if old_value is not None else None,
            new_value=new_value, reason=decision_reason,
        ))
    return RedirectResponse("/employees", status_code=303)


@app.post("/employees/{employee_id}/transfer", dependencies=[Depends(csrf_protect)])
def transfer_employee(
    employee_id: int,
    department_id: int = Form(...),
    branch_id: int | None = Form(None),
    effective_date: date = Form(...),
    reason: str = Form(...),
    admin: AdminAccount = Depends(require_system_admin),
):
    reason = reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="سبب النقل مطلوب.")
    if effective_date > date.today():
        raise HTTPException(
            status_code=400,
            detail="تاريخ سريان نقل الموظف لا يمكن أن يكون في المستقبل.",
        )
    with SessionLocal.begin() as db:
        employee = db.get(Employee, employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="الموظف غير موجود.")
        if employee.status != "active":
            raise HTTPException(status_code=409, detail="لا يمكن نقل موظف غير نشط.")
        department = db.get(Department, department_id)
        if department is None or not department.active:
            raise HTTPException(status_code=400, detail="القسم الجديد غير متاح.")
        branch = db.get(Branch, branch_id) if branch_id is not None else None
        if branch_id is not None and (branch is None or not branch.active or branch.department_id != department_id):
            raise HTTPException(status_code=400, detail="الفرع الجديد غير تابع للقسم المختار أو غير متاح.")
        old_department_id, old_branch_id = employee.department_id, employee.branch_id
        if old_department_id == department_id and old_branch_id == branch_id:
            raise HTTPException(status_code=409, detail="الموظف موجود بالفعل في القسم والفرع المحددين.")
        if effective_date < employee.hire_date:
            raise HTTPException(status_code=400, detail="تاريخ النقل لا يمكن أن يسبق تاريخ تعيين الموظف.")
        changes = [
            ("department_id", old_department_id, department_id),
            ("branch_id", old_branch_id, branch_id),
        ]
        employee.department_id = department_id
        employee.branch_id = branch_id
        for field_name, old_value, new_value in changes:
            if old_value != new_value:
                db.add(EmployeeChange(
                    employee_id=employee.id, field_name=field_name,
                    old_value=str(old_value) if old_value is not None else None,
                    new_value=str(new_value) if new_value is not None else None,
                    effective_date=effective_date, reason=reason,
                ))
        db.add(AuditLog(
            actor=admin.username, action="employee_transferred",
            entity_type="employee", entity_id=str(employee.id),
            old_value=f"department_id={old_department_id}; branch_id={old_branch_id}",
            new_value=f"department_id={department_id}; branch_id={branch_id}",
            reason=reason,
        ))
    return RedirectResponse("/employees", status_code=303)


@app.post("/employees/{employee_id}/archive", dependencies=[Depends(csrf_protect)])
def archive_employee(
    employee_id: int,
    reason: str = Form(...),
    admin: AdminAccount = Depends(require_system_admin),
):
    reason = reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="سبب الأرشفة مطلوب.")
    with SessionLocal.begin() as db:
        employee = db.get(Employee, employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="الموظف غير موجود.")
        if employee.status != "active":
            raise HTTPException(status_code=409, detail="الموظف ليس على رأس العمل.")
        period = db.scalar(
            select(EmployeeServicePeriod).where(
                EmployeeServicePeriod.employee_id == employee.id,
                EmployeeServicePeriod.end_date.is_(None),
            )
        )
        if period is None:
            raise HTTPException(status_code=409, detail="لا توجد فترة خدمة مفتوحة لهذا الموظف.")
        if date.today() < period.start_date:
            raise HTTPException(status_code=400, detail="لا يمكن إنهاء فترة خدمة قبل تاريخ بدايتها.")
        period.end_date = date.today()
        period.end_reason = reason
        employee.status = "archived"
        db.add(AuditLog(
            actor=admin.username, action="employee_archived",
            entity_type="employee", entity_id=str(employee.id),
            old_value="active", new_value="archived", reason=reason,
        ))
    return RedirectResponse("/employees", status_code=303)


@app.post("/employees/{employee_id}/rehire", dependencies=[Depends(csrf_protect)])
def rehire_employee(
    employee_id: int,
    new_hire_date: date = Form(...),
    reason: str = Form(...),
    admin: AdminAccount = Depends(require_system_admin),
):
    reason = reason.strip()
    if not reason:
        raise HTTPException(status_code=400, detail="سبب إعادة التعيين مطلوب.")
    with SessionLocal.begin() as db:
        employee = db.get(Employee, employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="الموظف غير موجود.")
        if employee.status != "archived":
            raise HTTPException(status_code=409, detail="لا يمكن إعادة تعيين موظف غير مؤرشف.")
        previous_period = db.scalar(
            select(EmployeeServicePeriod)
            .where(EmployeeServicePeriod.employee_id == employee.id)
            .order_by(EmployeeServicePeriod.id.desc())
            .limit(1)
        )
        if previous_period is None or previous_period.end_date is None:
            raise HTTPException(status_code=409, detail="سجل فترة الخدمة السابقة غير مكتمل؛ راجع بيانات الأرشفة أولاً.")
        if new_hire_date <= previous_period.end_date:
            raise HTTPException(status_code=400, detail="تاريخ إعادة التعيين يجب أن يكون بعد انتهاء فترة الخدمة السابقة.")
        employee.status = "active"
        employee.hire_date = new_hire_date
        db.add(EmployeeServicePeriod(
            employee_id=employee.id, start_date=new_hire_date, start_reason=reason,
        ))
        db.add(AuditLog(
            actor=admin.username, action="employee_rehired",
            entity_type="employee", entity_id=str(employee.id),
            old_value="archived", new_value="active", reason=reason,
        ))
    return RedirectResponse("/employees", status_code=303)
