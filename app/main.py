import asyncio
import math
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from uuid import uuid4
import secrets
import time as time_module

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text, func, or_
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.middleware.sessions import SessionMiddleware

from app.auth import authenticate, change_admin_password
from app.config import settings
from app.csrf import csrf_protect, get_csrf_token
from app.db import SessionLocal, engine, get_db
from app.dependencies import get_current_admin
from app.login_security import (
    FAILED_ATTEMPTS_LIMIT,
    LOCKOUT_MINUTES,
    clear_attempts,
    lockout_remaining,
    record_failed_attempt,
    send_login_alert,
)
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
from app.security import verify_password
from app.employee_codes import allocate_employee_code
from app.leave_workflow import add_balance_entry, cancel_leave, confirm_leave, leave_balance
from app.advance_workflow import approve_advance, create_advance, mark_advance_paid, reject_advance
from app.models import AdvanceInstallment, CAIRO_TZ
from app.national_id import ERROR_MESSAGE as NATIONAL_ID_ERROR, is_valid_egyptian_national_id
from app.staff_actions_workflow import approve_staff_action, cancel_staff_action, create_staff_action, record_employee_acknowledgement
from app.reports import build_hr_pdf, build_hr_workbook
from app.report_builder import (
    DEFAULT_FIELDS,
    FIELD_GROUP_DEFS,
    FIELD_GROUPS_BY_TYPE,
    PAYROLL_BREAKDOWN_DEFS,
    REPORT_TYPE_COMPREHENSIVE,
    REPORT_TYPE_LABELS,
    REPORT_TYPES,
    ReportSelection,
    _parse_date,
    apply_config,
    build_custom_pdf,
    build_custom_workbook,
    build_report,
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
    add_payroll_payment,
    approve_payroll_run,
    close_payroll_run,
    create_payroll_run,
    approve_overtime,
    payment_totals,
    reopen_payroll_run,
    update_payroll_item,
)
from app.models import AdvanceRequest, BackupRun, SystemSetting, StaffAction, PayrollRun, PayrollItem, PayrollPayment, AdminAccount, AttendanceDailyRecord, AttendanceImportBatch, AttendanceImportIssue, AttendancePunch, AttendanceMonth, AuditLog, Branch, Department, Employee, EmployeeChange, EmployeeDataChangeRequest, EmployeeServicePeriod, LeaveBalanceTransaction, LeaveRecord, LeaveType
from app.security_roster_import import parse_security_roster_xlsx
from app.attendance_import import parse_attendance_workbook, normalize as normalize_attendance_alias
from app.attendance_workflow import persist_attendance_import, prepare_and_persist_monthly_reconciliation, approve_attendance_day, approve_attendance_month, add_manual_attendance, resolve_unknown_attendance_employee, summarize_attendance_month_coverage
from app.security_roster_workflow import prepare_security_roster, confirm_security_roster_day
from app.ai_assistant import ask_local_model, local_fallback_answer, local_model_status
from app.ai_agent import is_command_request
from app.ai_hr_queries import is_structured_hr_query, answer_structured_hr_query
from app.ai_gateway import prepare_command, approve_command, reject_command, recent_memory

SECURITY_ROSTER_MAX_BYTES = 5 * 1024 * 1024
SECURITY_ROSTER_STAGING_DIR = Path("var") / "security_roster_staging"
SECURITY_ROSTER_STAGING_TTL_SECONDS = 60 * 60
SECURITY_ROSTER_CLEANUP_INTERVAL_SECONDS = 5 * 60
REPORTS_DIR = Path("var") / "reports"


def _save_report_copy(content: bytes, extension: str, start_date: date, end_date: date, employee_id: int | None = None) -> Path:
    """Save a user-requested report locally without replacing an earlier export."""
    if extension not in {"xlsx", "pdf"} or not isinstance(content, bytes) or not content:
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
        filename = f"MY_HR_Report_{start_date.isoformat()}_{end_date.isoformat()}_{employee_part}_{stamp}_{uuid4().hex[:8]}.{extension}"
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


templates.env.filters["fmt_dt"] = _fmt_dt
templates.env.filters["fmt_d"] = _fmt_d
templates.env.filters["fmt_m"] = _fmt_m
templates.env.filters["status_label"] = _status_label
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



@app.exception_handler(HTTPException)
async def authentication_exception_handler(request: Request, exc: HTTPException):
    """Redirect browser page requests to login while preserving API error responses."""
    accept = request.headers.get("accept", "").lower()
    wants_html = "text/html" in accept and "application/json" not in accept
    if exc.status_code == 401 and request.method in {"GET", "HEAD"} and wants_html:
        return RedirectResponse("/login", status_code=303)
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
    context = {"error": None, "csrf_token": get_csrf_token(request)}
    with SessionLocal() as db:
        remaining = lockout_remaining(db)
    if remaining is not None:
        context["error"] = "تم إيقاف الدخول مؤقتًا بعد كثرة المحاولات غير الصحيحة. حاول مرة أخرى بعد"
        context["lockout_minutes"] = max(1, math.ceil(remaining.total_seconds() / 60))
        context["lockout_unit"] = "دقيقة."
    return templates.TemplateResponse(
        request=request, name="login.html",
        context=context,
    )


@app.post("/login", dependencies=[Depends(csrf_protect)])
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with SessionLocal() as db:
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
        clear_attempts(db, actor="login")
        staged_token = request.session.get("security_roster_stage")
        if isinstance(staged_token, str):
            _clear_staged_security_roster(staged_token, request)
        request.session.clear()
        request.session["admin_username"] = admin.username
        request.session["admin_session_version"] = admin.session_version
        get_csrf_token(request)
    send_login_alert("login_success", admin.username)
    send_telegram_login_event("login_success", admin.username, app_name=settings.app_name)
    return RedirectResponse("/", status_code=303)


@app.post("/logout", dependencies=[Depends(csrf_protect)])
def logout(request: Request):
    staged_token = request.session.get("security_roster_stage")
    if isinstance(staged_token, str):
        _clear_staged_security_roster(staged_token, request)
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, admin: AdminAccount = Depends(get_current_admin)):
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
                "admin": admin.username,
                "csrf_token": get_csrf_token(request),
                "metrics": metrics,
                "alerts": alerts,
            },
        )


@app.get("/ai", response_class=HTMLResponse)
def ai_page(request: Request, admin: AdminAccount = Depends(get_current_admin)):
    with SessionLocal() as db:
        metrics = {
            "active_employees": db.scalar(select(func.count(Employee.id)).where(Employee.status == "active")) or 0,
            "former_employees": db.scalar(select(func.count(Employee.id)).where(Employee.status != "active")) or 0,
            "attendance_review": db.scalar(
                select(func.count(AttendanceDailyRecord.id)).where(
                    AttendanceDailyRecord.status.in_(["needs_review", "ready_for_review"])
                )
            ) or 0,
            "leave_drafts": db.scalar(select(func.count(LeaveRecord.id)).where(LeaveRecord.status == "draft")) or 0,
            "advance_pending": db.scalar(
                select(func.count(AdvanceRequest.id)).where(AdvanceRequest.status.in_(["pending", "approved"]))
            ) or 0,
            "payroll_drafts": db.scalar(
                select(func.count(PayrollRun.id)).where(PayrollRun.status.in_(["draft", "reopened"]))
            ) or 0,
        }
    return templates.TemplateResponse(
        request=request,
        name="ai.html",
        context={
            "admin": admin.username,
            "csrf_token": get_csrf_token(request),
            "status": local_model_status(),
            "question": "",
            "answer": None,
        },
    )


@app.post("/ai", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def ai_ask(
    request: Request,
    question: str = Form(...),
    admin: AdminAccount = Depends(get_current_admin),
):
    question = question.strip()
    if not question or len(question) > 2000:
        raise HTTPException(status_code=400, detail="اكتب أمرًا أو سؤالًا من 1 إلى 2000 حرف.")

    pending = None
    answer = None
    if is_command_request(question):
        try:
            result = prepare_command(question, admin.username)
            pending = {
                "token": result["token"],
                "summary": result["summary"],
                "plan": result["plan"],
                "old_values": result.get("old_values", {}),
                "new_values": result.get("new_values", {}),
                "risks": result.get("risks", []),
                "expires_at": result["expires_at"].isoformat(),
            }
            answer = "تم إعداد خطة التنفيذ. لن يتم أي تعديل قبل موافقتك الصريحة."
        except (RuntimeError, ValueError, SQLAlchemyError) as exc:
            answer = f"لم يتم إعداد العملية: {exc}"
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
                answer = answer_structured_hr_query(question)
            except (RuntimeError, ValueError, SQLAlchemyError) as exc:
                answer = f"تعذر قراءة بيانات الموارد البشرية المطلوبة: {exc}"
        else:
            try:
                answer = ask_local_model(question, context)
            except RuntimeError:
                answer = local_fallback_answer(question, context)

    return templates.TemplateResponse(
        request=request,
        name="ai.html",
        context={
            "admin": admin.username,
            "csrf_token": get_csrf_token(request),
            "status": local_model_status(),
            "question": question,
            "answer": answer,
            "pending": pending,
            "memory": recent_memory(admin.username),
        },
    )


@app.post("/ai/approve", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def ai_approve(
    request: Request,
    token: str = Form(...),
    admin: AdminAccount = Depends(get_current_admin),
):
    try:
        answer = approve_command(token.strip(), admin.username)
    except (RuntimeError, ValueError, SQLAlchemyError) as exc:
        answer = f"لم يتم تنفيذ العملية: {exc}"
    return templates.TemplateResponse(
        request=request,
        name="ai.html",
        context={
            "admin": admin.username,
            "csrf_token": get_csrf_token(request),
            "status": local_model_status(),
            "question": "",
            "answer": answer,
            "pending": None,
            "memory": recent_memory(admin.username),
        },
    )


@app.post("/ai/reject", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def ai_reject(
    request: Request,
    token: str = Form(...),
    admin: AdminAccount = Depends(get_current_admin),
):
    try:
        answer = reject_command(token.strip(), admin.username)
    except (RuntimeError, ValueError, SQLAlchemyError) as exc:
        answer = f"لم يتم رفض العملية: {exc}"
    return templates.TemplateResponse(
        request=request,
        name="ai.html",
        context={
            "admin": admin.username,
            "csrf_token": get_csrf_token(request),
            "status": local_model_status(),
            "question": "",
            "answer": answer,
            "pending": None,
            "memory": recent_memory(admin.username),
        },
    )


@app.get("/security-roster", response_class=HTMLResponse)
def security_roster_page(
    request: Request,
    admin: AdminAccount = Depends(get_current_admin),
):
    return templates.TemplateResponse(
        request=request,
        name="security_roster.html",
        context={
            "admin": admin.username,
            "csrf_token": get_csrf_token(request),
            "days": [],
            "issues": [],
            "error": None,
            "filename": None,
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
):
    return templates.TemplateResponse(
        request=request,
        name="attendance_import.html",
        context={"admin": admin.username, "csrf_token": get_csrf_token(request),
                 "error": None, "batch": None, "issues": []},
    )


@app.post("/attendance/import", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def attendance_import_upload(
    request: Request,
    source_type: str = Form(...),
    file: UploadFile = File(...),
    admin: AdminAccount = Depends(get_current_admin),
):
    context = {
        "admin": admin.username, "csrf_token": get_csrf_token(request),
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
    admin: AdminAccount = Depends(get_current_admin),
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
            "admin": admin.username, "csrf_token": get_csrf_token(request),
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
    admin: AdminAccount = Depends(get_current_admin),
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
                "admin": admin.username, "csrf_token": get_csrf_token(request),
                "batch": batch, "issues": issues, "employees": employees,
                "error": str(exc),
            },
            status_code=400,
        )
    return RedirectResponse(f"/attendance/import/{batch_id}/issues", status_code=303)


@app.get("/attendance/reconcile", response_class=HTMLResponse)
def attendance_reconcile_page(
    request: Request,
    admin: AdminAccount = Depends(get_current_admin),
):
    batches = _attendance_import_batch_options()
    return templates.TemplateResponse(
        request=request,
        name="attendance_reconcile.html",
        context={
            "admin": admin.username, "csrf_token": get_csrf_token(request),
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
    admin: AdminAccount = Depends(get_current_admin),
):
    context = {
        "admin": admin.username, "csrf_token": get_csrf_token(request),
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
    admin: AdminAccount = Depends(get_current_admin),
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
        records = []
        for record, employee in rows:
            # Manual HR evidence is the latest explicit correction and must
            # be visible as the default review proposal, without replacing raw punches.
            suggested_in = record.manual_clock_in or record.program_clock_in or record.first_clock_in
            suggested_out = record.manual_clock_out or record.program_clock_out or record.last_clock_out
            if (
                suggested_in is not None and suggested_out is not None
                and suggested_out <= suggested_in
                and record.scheduled_start_at is not None
                and record.scheduled_end_at is not None
                and record.scheduled_end_at.date() > record.scheduled_start_at.date()
                and suggested_out.date() == record.work_date
            ):
                suggested_out += timedelta(days=1)
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
            })
    employees = db.scalars(
        select(Employee).where(Employee.status == "active").order_by(Employee.full_name, Employee.id)
    ).all()
    return templates.TemplateResponse(
        request=request, name="attendance_review.html",
        context={"admin": admin.username, "csrf_token": get_csrf_token(request),
                 "records": records, "employees": employees, "error": None, "page": page,
                 "total_pages": total_pages, "total_records": total_records,
                 "page_size": page_size},
    )


@app.post("/attendance/manual", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def attendance_manual_entry(
    request: Request,
    employee_id: int = Form(...),
    work_date: str = Form(...),
    clock_in: str = Form(...),
    clock_out: str = Form(...),
    reason: str = Form(...),
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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


@app.get("/attendance/months", response_class=HTMLResponse)
def attendance_months_page(
    request: Request,
    admin: AdminAccount = Depends(get_current_admin),
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
        context={"admin": admin.username, "csrf_token": get_csrf_token(request),
                 "months": summaries, "error": None},
    )


@app.post("/attendance/months/{month_id}/approve", response_class=HTMLResponse, dependencies=[Depends(csrf_protect)])
def attendance_month_approve(
    month_id: int,
    request: Request,
    reason: str = Form(...),
    coverage_confirmed: bool = Form(False),
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
                "admin": admin.username,
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
                "admin": admin.username,
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
    admin: AdminAccount = Depends(get_current_admin),
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
                "admin": admin.username,
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
                "admin": admin.username,
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
):
    """Serve an employee photo only to an authenticated administrator."""
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
def leave_page(request: Request, admin: AdminAccount = Depends(get_current_admin)):
    with SessionLocal() as db:
        employees = db.scalars(
            select(Employee).where(Employee.status == "active").order_by(Employee.code)
        ).all()
        leave_types = db.scalars(
            select(LeaveType).where(LeaveType.active.is_(True)).order_by(LeaveType.id)
        ).all()
        records = db.scalars(
            select(LeaveRecord).order_by(LeaveRecord.created_at.desc(), LeaveRecord.id.desc())
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
def advances_page(request: Request, admin: AdminAccount = Depends(get_current_admin)):
    with SessionLocal() as db:
        employees = db.scalars(
            select(Employee).where(Employee.status == "active").order_by(Employee.code)
        ).all()
        all_employees = db.scalars(select(Employee).order_by(Employee.code)).all()
        requests = db.scalars(
            select(AdvanceRequest).order_by(
                AdvanceRequest.request_date.desc(), AdvanceRequest.id.desc()
            )
        ).all()
        advance_summary = {}
        for employee in employees:
            employee_requests = [item for item in requests if item.employee_id == employee.id]
            regular_outstanding = Decimal("0.00")
            installment_outstanding = Decimal("0.00")
            deducted = Decimal("0.00")
            for item in employee_requests:
                applied = db.scalar(
                    select(func.coalesce(func.sum(AdvanceInstallment.amount), 0)).where(
                        AdvanceInstallment.advance_request_id == item.id,
                        AdvanceInstallment.status == "applied",
                    )
                ) or Decimal("0.00")
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
        return templates.TemplateResponse(
            request=request,
            name="advances.html",
            context={
                "employees": employees,
                "requests": requests,
                "advance_summary": advance_summary,
                "employee_names": {e.id: e.full_name for e in all_employees},
                "employee_codes": {e.id: e.code for e in all_employees},
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
def staff_actions_page(request: Request, admin: AdminAccount = Depends(get_current_admin)):
    with SessionLocal() as db:
        employees = db.scalars(
            select(Employee).where(Employee.status == "active").order_by(Employee.code)
        ).all()
        all_employees = db.scalars(select(Employee).order_by(Employee.code)).all()
        actions = db.scalars(
            select(StaffAction).order_by(StaffAction.created_at.desc(), StaffAction.id.desc())
        ).all()
        return templates.TemplateResponse(
            request=request,
            name="staff_actions.html",
            context={
                "employees": employees,
                "actions": actions,
                "employee_names": {e.id: e.full_name for e in all_employees},
                "employee_codes": {e.id: e.code for e in all_employees},
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
def payroll_page(request: Request, admin: AdminAccount = Depends(get_current_admin)):
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
                "csrf_token": get_csrf_token(request),
                "error": request.query_params.get("error"),
            },
        )


@app.get("/payroll/runs/{run_id}/items/{item_id}/payslip.pdf")
def payroll_item_payslip(
    run_id: int,
    item_id: int,
    request: Request,
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
):
    try:
        with SessionLocal.begin() as db:
            run = db.scalar(
                select(PayrollRun).where(PayrollRun.id == run_id).with_for_update()
            )
            if run is None:
                return RedirectResponse("/payroll?error=run", status_code=303)
            approve_payroll_run(db, run, admin.username)
    except (ValueError, SQLAlchemyError):
        return RedirectResponse(f"/payroll/runs/{run_id}?error=approve", status_code=303)
    return RedirectResponse(f"/payroll/runs/{run_id}", status_code=303)


@app.post("/payroll/runs/{run_id}/close", dependencies=[Depends(csrf_protect)])
def close_payroll_run_route(
    run_id: int,
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
        elif active_preset is not None:
            selection = ReportSelection.from_config(preset_config(active_preset))
        start, end = resolve_period(selection)
        employees = db.scalars(select(Employee).order_by(Employee.code)).all()
        departments = db.scalars(select(Department).order_by(Department.name)).all()
        presets = list_presets(db)
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
            "report_types": REPORT_TYPES,
            "report_type_labels": REPORT_TYPE_LABELS,
            "csrf_token": get_csrf_token(request),
            "error": params.get("error"),
            "saved": params.get("saved"),
            "deleted": params.get("deleted"),
        },
    )


@app.get("/reports/view", response_class=HTMLResponse)
def view_hr_report(
    request: Request,
    admin: AdminAccount = Depends(get_current_admin),
):
    selection = selection_from_params(request.query_params)
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
            db.delete(preset)
            db.commit()
    return RedirectResponse("/reports?deleted=1", status_code=303)


def _custom_report_requested(params) -> bool:
    return bool(params.get("report_type") or params.get("fields"))


@app.get("/reports/export.xlsx")
def export_hr_reports(
    request: Request,
    start_date: date,
    end_date: date,
    employee_id: int | None = None,
    admin: AdminAccount = Depends(get_current_admin),
):
    params = request.query_params
    try:
        with SessionLocal() as db:
            if _custom_report_requested(params):
                selection = selection_from_params(params)
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
    try:
        with SessionLocal() as db:
            if _custom_report_requested(params):
                selection = selection_from_params(params)
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
        context={"error": None, "success": False, "csrf_token": get_csrf_token(request), **_telegram_context(request)},
    )


@app.post("/settings/telegram", dependencies=[Depends(csrf_protect)])
def telegram_settings_save(
    request: Request,
    bot_token: str = Form(""),
    chat_id: str = Form(""),
    language: str = Form("ar"),
    enabled: str = Form(""),
    admin: AdminAccount = Depends(get_current_admin),
):
    with SessionLocal.begin() as db:
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
def telegram_bind_link(request: Request, admin: AdminAccount = Depends(get_current_admin)):
    with SessionLocal.begin() as db:
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
def telegram_bind_confirm(request: Request, admin: AdminAccount = Depends(get_current_admin)):
    with SessionLocal.begin() as db:
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
def telegram_test_message(request: Request, admin: AdminAccount = Depends(get_current_admin)):
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
            context={"error": error, "success": success, "csrf_token": get_csrf_token(request)},
            status_code=status_code,
        )

    if new_password != confirm_password:
        return render("mismatch", status_code=400)
    with SessionLocal.begin() as db:
        db_admin = db.get(AdminAccount, admin.id)
        if db_admin is None or not verify_password(current_password, db_admin.password_hash):
            return render("wrong_current", status_code=400)
        if verify_password(new_password, db_admin.password_hash):
            return render("same", status_code=400)
        try:
            change_admin_password(db, db_admin, new_password)
        except ValueError:
            return render("too_short", status_code=400)
        db.add(AuditLog(
            actor=admin.username,
            action="admin_password_changed",
            entity_type="admin_account",
            entity_id="admin",
        ))
        new_session_version = db_admin.session_version
    # The rotation invalidates every other session; keep this one signed in.
    request.session["admin_session_version"] = new_session_version
    return render(None, success=True)


AUDIT_PAGE_SIZE = 50


@app.get("/settings/audit", response_class=HTMLResponse)
def audit_log_page(
    request: Request,
    q: str = "",
    entity_type: str = "",
    from_date: str = "",
    to_date: str = "",
    page: int = 1,
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    db: Session = Depends(get_db),
    admin: AdminAccount = Depends(get_current_admin),
):
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
    admin: AdminAccount = Depends(get_current_admin),
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
            context={"departments": departments, "branches": branches, "all_departments": all_departments, "all_branches": all_branches, "employee_changes": employee_changes, "data_change_requests": data_change_requests, "employees": employees, "csrf_token": get_csrf_token(request), "q": query, "filter_department_id": department_id, "filter_branch_id": branch_id, "filter_status": status, "filters_active": bool(query or department_id or branch_id or status), "page": page, "pages": pages, "total": total, "page_url": page_url, "req_page": req_page, "req_pages": req_pages, "req_page_url": req_page_url},
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
    admin: AdminAccount = Depends(get_current_admin),
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
