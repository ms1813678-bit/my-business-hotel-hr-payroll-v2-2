from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, ForeignKeyConstraint, Index, Integer, LargeBinary, Numeric, String, Text, Time, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.sync_uid_mixin import SyncUidMixin, sync_uid_constraints

# Single hotel deployment: all business timestamps are interpreted in Cairo
# local time (winter UTC+2, summer UTC+3), stored as absolute UTC instants.
CAIRO_TZ = ZoneInfo("Africa/Cairo")


def utcnow() -> datetime:
    """Current UTC instant, timezone-aware, safe for DateTime(timezone=True) columns."""
    return datetime.now(timezone.utc)


class Department(SyncUidMixin, Base):
    __tablename__ = "departments"
    __table_args__ = sync_uid_constraints("departments")
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    employees: Mapped[list["Employee"]] = relationship(back_populates="department")


class Branch(Base):
    __tablename__ = "branches"
    id: Mapped[int] = mapped_column(primary_key=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    department: Mapped[Department] = relationship()
    __table_args__ = (UniqueConstraint("department_id", "name"),)


class Employee(SyncUidMixin, Base):
    __tablename__ = "employees"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(200), index=True)
    national_id: Mapped[str | None] = mapped_column(String(20), unique=True)
    nationality: Mapped[str | None] = mapped_column(String(10), default="egyptian")
    passport_number: Mapped[str | None] = mapped_column(String(20), unique=True)
    phone: Mapped[str | None] = mapped_column(String(30))
    address: Mapped[str | None] = mapped_column(Text)
    photo_path: Mapped[str | None] = mapped_column(String(255))
    fingerprint_id: Mapped[str | None] = mapped_column(String(50), unique=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"))
    branch_id: Mapped[int | None] = mapped_column(ForeignKey("branches.id"))
    position: Mapped[str] = mapped_column(String(120))
    hire_date: Mapped[date] = mapped_column(Date)
    # Leave foundation: unknown values stay NULL, never inferred from identifiers.
    birth_date: Mapped[date | None] = mapped_column(Date)
    prior_insurance_years: Mapped[Decimal | None] = mapped_column(Numeric(8, 4))
    __table_args__ = (
        CheckConstraint("prior_insurance_years IS NULL OR prior_insurance_years >= 0",
                        name="ck_employee_prior_insurance_nonnegative"),
        *sync_uid_constraints("employees"),
    )
    probation_start_date: Mapped[date | None] = mapped_column(Date)
    probation_end_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="active")
    basic_salary: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    daily_hours: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    department: Mapped[Department] = relationship(back_populates="employees")

  
class ShiftPattern(Base):
    __tablename__ = "shift_patterns"
    __table_args__ = (
        UniqueConstraint("department_id", "code", name="uq_shift_patterns_department_code"),
        CheckConstraint(
            "match_window_minutes IS NULL OR match_window_minutes >= 0",
            name="ck_shift_patterns_match_window_nonnegative",
        ),
        CheckConstraint(
            "match_before_minutes BETWEEN 0 AND 120",
            name="ck_shift_patterns_match_before_range",
        ),
        CheckConstraint(
            "match_after_minutes BETWEEN 0 AND 120",
            name="ck_shift_patterns_match_after_range",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    department_id: Mapped[int] = mapped_column(ForeignKey("departments.id"), index=True)
    code: Mapped[str] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(100))
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    # Legacy symmetric margin retained for migration compatibility; new matching uses the separate bounds.
    match_window_minutes: Mapped[int | None] = mapped_column(Integer)
    match_before_minutes: Mapped[int | None] = mapped_column(Integer)
    match_after_minutes: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class SecurityShiftAssignment(Base):
    __tablename__ = "security_shift_assignments"
    __table_args__ = (
        UniqueConstraint("employee_id", "work_date", name="uq_security_shift_employee_date"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)
    shift_pattern_id: Mapped[int] = mapped_column(ForeignKey("shift_patterns.id"))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EmployeeCodeCounter(Base):
    __tablename__ = "employee_code_counters"
    __table_args__ = (
        CheckConstraint("id = 1", name="ck_employee_code_counter_singleton"),
        CheckConstraint("last_number >= 0", name="ck_employee_code_counter_nonnegative"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_number: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EmployeeServicePeriod(Base):
    __tablename__ = "employee_service_periods"
    __table_args__ = (
        CheckConstraint(
            "end_date IS NULL OR end_date >= start_date",
            name="ck_employee_service_period_dates",
        ),
        Index(
            "uq_employee_one_open_service_period", "employee_id", unique=True,
            postgresql_where=(__import__("sqlalchemy").text("end_date IS NULL")),
            sqlite_where=(__import__("sqlalchemy").text("end_date IS NULL")),
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    start_reason: Mapped[str] = mapped_column(Text, default="Initial employment")
    end_reason: Mapped[str | None] = mapped_column(Text)


class EmployeeChange(Base):
    __tablename__ = "employee_changes"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    field_name: Mapped[str] = mapped_column(String(80))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    effective_date: Mapped[date] = mapped_column(Date)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reason: Mapped[str] = mapped_column(Text)


class EmployeeDataChangeRequest(Base):
    __tablename__ = "employee_data_change_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id"), index=True)
    field_name: Mapped[str] = mapped_column(String(40))
    requested_value: Mapped[str] = mapped_column(Text)
    request_reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    decision_reason: Mapped[str | None] = mapped_column(Text)
    effective_date: Mapped[date | None] = mapped_column(Date)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[str | None] = mapped_column(String(80))


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(80), default="system_admin")
    action: Mapped[str] = mapped_column(String(100), index=True)
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String(80))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class AdminSession(Base):
    """Server-side record of one signed-in session so open sessions can be
    listed and individually terminated. The signed cookie carries this id;
    deleting the row ends the session. user_agent/ip_address are informational
    metadata from the request, not verified device identity."""

    __tablename__ = "admin_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    admin_account_id: Mapped[int] = mapped_column(ForeignKey("admin_accounts.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user_agent: Mapped[str | None] = mapped_column(String(300))
    ip_address: Mapped[str | None] = mapped_column(String(45))


class RecoveryCode(Base):
    """One-use forgotten-password recovery code for the system administrator.

    Only the Argon2 hash is stored; the readable code is shown to the owner
    exactly once at generation. Used or regenerated codes can never recover
    the account again."""

    __tablename__ = "recovery_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_account_id: Mapped[int] = mapped_column(ForeignKey("admin_accounts.id"), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    # Keyed HMAC of the normalized code for O(1) lookup: recovery finds the
    # one candidate row without verifying every hash, keeping failure timing
    # identical for unknown users and wrong codes (one verify either way).
    code_lookup: Mapped[str | None] = mapped_column(String(64), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


@event.listens_for(AuditLog, "before_update")
def prevent_audit_log_update(mapper, connection, target):
    raise ValueError("Audit log records are immutable.")


@event.listens_for(AuditLog, "before_delete")
def prevent_audit_log_deletion(mapper, connection, target):
    raise ValueError("Audit log records cannot be deleted.")


ACCOUNT_ROLES = ("system_admin", "finance", "assistant_manager")


class AdminAccount(Base):
    """A named login account. Exactly one historical row existed (the HR
    manager / system administrator); named assistant-manager and finance
    accounts were added later. Accounts are never deleted (audit history
    depends on the actor name); they are deactivated instead, and at least
    one active system administrator must always remain."""

    __tablename__ = "admin_accounts"
    __table_args__ = (
        CheckConstraint(
            "role IN ('system_admin', 'finance', 'assistant_manager')",
            name="ck_admin_accounts_role",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    singleton_key: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)
    username: Mapped[str] = mapped_column(String(80), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    # Person's real name shown in audit and account lists (accounts are named
    # after their people per the owner's account policy).
    display_name: Mapped[str | None] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(20), default="system_admin", server_default="system_admin", nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    session_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    # Set when a backup restore resurrects this account from archive time
    # (deleted after the backup, or absent from a fresh install's snapshot):
    # the account stays inactive until the system administrator issues a NEW
    # password through the audited reset flow, which also clears this flag.
    # Plain re-activation is refused so the archive-time password can never
    # authenticate again (owner-approved 2026-11-23).
    pending_credential_reset: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)


@event.listens_for(AdminAccount, "before_delete")
def prevent_admin_deletion(mapper, connection, target):
    raise ValueError("Accounts cannot be deleted; deactivate them instead.")


class AttendanceImportBatch(Base):
    """Immutable source-file receipt and processing state for monthly attendance imports."""
    __tablename__ = "attendance_import_batches"
    __table_args__ = (
        CheckConstraint(
            "source_type IN ('program_report', 'fingerprint')",
            name="ck_attendance_import_source_type",
        ),
        CheckConstraint(
            "status IN ('uploaded', 'parsed', 'needs_review', 'approved', 'rejected')",
            name="ck_attendance_import_status",
        ),
        CheckConstraint("period_end >= period_start", name="ck_attendance_import_period"),
        CheckConstraint("row_count >= 0 AND issue_count >= 0", name="ck_attendance_import_counts"),
        UniqueConstraint("source_type", "file_sha256", name="uq_attendance_import_source_hash"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    source_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_end: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    original_file_bytes: Mapped[bytes | None] = mapped_column(LargeBinary)
    uploaded_by: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="uploaded", nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    issue_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    reviewed_by: Mapped[str | None] = mapped_column(String(80))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AttendancePunch(Base):
    """Parsed fingerprint event; raw source identity and cell text are retained for traceability."""
    __tablename__ = "attendance_punches"
    __table_args__ = (
        UniqueConstraint(
            "import_batch_id", "source_sheet", "source_row", "event_index",
            name="uq_attendance_punch_source_position",
        ),
        CheckConstraint("event_index >= 0", name="ck_attendance_punch_event_index"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(
        ForeignKey("attendance_import_batches.id", ondelete="RESTRICT"), index=True
    )
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), index=True
    )
    raw_employee_code: Mapped[str | None] = mapped_column(String(80))
    raw_employee_name: Mapped[str | None] = mapped_column(String(200))
    punch_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), index=True)
    raw_timestamp: Mapped[str] = mapped_column(String(120), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(40))
    source_sheet: Mapped[str] = mapped_column(String(120), nullable=False)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)
    event_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AttendanceMonth(Base):
    """Monthly attendance lifecycle; daily approvals alone never close a period."""
    __tablename__ = "attendance_months"
    __table_args__ = (
        CheckConstraint("period_end >= period_start", name="ck_attendance_month_period"),
        CheckConstraint(
            "status IN ('imported', 'under_review', 'approved')",
            name="ck_attendance_month_status",
        ),
        UniqueConstraint("period_start", "period_end", name="uq_attendance_month_period"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    program_import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("attendance_import_batches.id", ondelete="RESTRICT")
    )
    fingerprint_import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("attendance_import_batches.id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(24), default="imported", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    reviewed_by: Mapped[str | None] = mapped_column(String(80))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(String(80))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approval_reason: Mapped[str | None] = mapped_column(Text)


class AttendanceDailyRecord(Base):
    """One employee/work-date reconciliation result, pending HR approval by default."""
    __tablename__ = "attendance_daily_records"
    __table_args__ = (
        UniqueConstraint("employee_id", "work_date", name="uq_attendance_daily_employee_date"),
        CheckConstraint(
            "status IN ('needs_review', 'ready_for_review', 'approved', 'rejected')",
            name="ck_attendance_daily_status",
        ),
        CheckConstraint(
            "scheduled_end_at IS NULL OR scheduled_start_at IS NULL "
            "OR scheduled_end_at > scheduled_start_at",
            name="ck_attendance_daily_schedule_order",
        ),
        CheckConstraint("worked_minutes IS NULL OR worked_minutes >= 0", name="ck_attendance_daily_worked_minutes"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), index=True
    )
    work_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    shift_pattern_id: Mapped[int | None] = mapped_column(ForeignKey("shift_patterns.id"))
    security_shift_assignment_id: Mapped[int | None] = mapped_column(
        ForeignKey("security_shift_assignments.id", ondelete="RESTRICT")
    )
    program_import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("attendance_import_batches.id", ondelete="RESTRICT")
    )
    fingerprint_import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("attendance_import_batches.id", ondelete="RESTRICT")
    )
    scheduled_start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    scheduled_end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    program_clock_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    program_clock_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    approved_clock_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    approved_clock_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    first_clock_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    last_clock_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    manual_clock_in: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    manual_clock_out: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    manual_entry_reason: Mapped[str | None] = mapped_column(Text)
    manual_entered_by: Mapped[str | None] = mapped_column(String(80))
    manual_entered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    worked_minutes: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="needs_review", nullable=False)
    review_notes: Mapped[str | None] = mapped_column(Text)
    approved_by: Mapped[str | None] = mapped_column(String(80))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AttendanceImportIssue(Base):
    """A source-row parsing or identity problem retained for explicit HR review."""
    __tablename__ = "attendance_import_issues"
    __table_args__ = (
        CheckConstraint("source_row >= 0", name="ck_attendance_import_issue_source_row"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    import_batch_id: Mapped[int] = mapped_column(
        ForeignKey("attendance_import_batches.id", ondelete="RESTRICT"), index=True
    )
    source_sheet: Mapped[str | None] = mapped_column(String(120))
    source_row: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(100), nullable=False)
    raw_value: Mapped[str | None] = mapped_column(Text)
    details: Mapped[str | None] = mapped_column(Text)
    resolved_employee_id: Mapped[int | None] = mapped_column(ForeignKey("employees.id"))
    resolved_by: Mapped[str | None] = mapped_column(String(80))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)



class LeaveType(SyncUidMixin, Base):
    __tablename__ = "leave_types"
    __table_args__ = (
        UniqueConstraint("code", name="uq_leave_types_code"),
        CheckConstraint("length(trim(code)) > 0", name="ck_leave_types_code_nonempty"),
        *sync_uid_constraints("leave_types"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(40), nullable=False)
    name_ar: Mapped[str] = mapped_column(String(100), nullable=False)
    name_en: Mapped[str] = mapped_column(String(100), nullable=False)
    name_zh: Mapped[str | None] = mapped_column(String(100))
    is_paid: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    tracks_balance: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class LeaveRecord(SyncUidMixin, Base):
    __tablename__ = "leave_records"
    __table_args__ = (
        CheckConstraint("end_date >= start_date", name="ck_leave_records_date_range"),
        CheckConstraint("calendar_days >= 1", name="ck_leave_records_calendar_days"),
        CheckConstraint("charged_days > 0", name="ck_leave_records_charged_days"),
        CheckConstraint(
            "status IN ('draft', 'confirmed', 'cancelled')",
            name="ck_leave_records_status",
        ),
        *sync_uid_constraints("leave_records"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    leave_type_id: Mapped[int] = mapped_column(
        ForeignKey("leave_types.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    end_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    calendar_days: Mapped[int] = mapped_column(Integer, nullable=False)
    charged_days: Mapped[Decimal] = mapped_column(Numeric(), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # NULL is unknown for legacy records; false is a deliberate declaration.
    certificate_attached: Mapped[bool | None] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    confirmed_by: Mapped[str | None] = mapped_column(String(80))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[str | None] = mapped_column(String(80))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(Text)


class LeaveHoliday(Base):
    """HR-entered calendar facts; credits are posted separately and idempotently."""
    __tablename__ = "leave_holidays"
    __table_args__ = (CheckConstraint("length(trim(name)) > 0", name="ck_leave_holiday_name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LeaveRegisterEntry(Base):
    """Evidence-preserving day register. NULL is unknown, empty is explicit present.

    No attendance inference is made by this table. A write service must enforce
    service dates, precedence, operator authorization, month close and audit.
    """
    __tablename__ = "leave_register_entries"
    __table_args__ = (
        UniqueConstraint("employee_id", "work_date", name="uq_leave_register_employee_day"),
        CheckConstraint("code IS NULL OR code IN ('', 'U', 'A', 'S', 'H', 'W', '—')",
                        name="ck_leave_register_code"),
        CheckConstraint("length(trim(source)) > 0", name="ck_leave_register_source"),
        CheckConstraint("revision >= 1", name="ck_leave_register_revision"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False, index=True)
    work_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    code: Mapped[str | None] = mapped_column(String(1))
    certificate_attached: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(80), nullable=False)
    leave_record_id: Mapped[int | None] = mapped_column(ForeignKey("leave_records.id", ondelete="RESTRICT"))
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    recorded_by: Mapped[str] = mapped_column(String(80), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LeaveMonthNote(Base):
    __tablename__ = "leave_month_notes"
    __table_args__ = (
        UniqueConstraint("employee_id", "year", "month", name="uq_leave_month_note_employee_period"),
        CheckConstraint("month BETWEEN 1 AND 12", name="ck_leave_month_note_month"),
        CheckConstraint("year BETWEEN 1 AND 9999", name="ck_leave_month_note_year"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_by: Mapped[str] = mapped_column(String(80), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LeaveMonthClose(Base):
    """Closure fact, never set merely because a calendar date arrived.

    Write workflow must verify the referenced host backup, authorize the actual
    operator, audit and serialize changes. No workflow is enabled by this model.
    """
    __tablename__ = "leave_month_close"
    __table_args__ = (
        UniqueConstraint("year", "month", name="uq_leave_close_period"),
        CheckConstraint("month BETWEEN 1 AND 12", name="ck_leave_close_month"),
        CheckConstraint("year BETWEEN 1 AND 9999", name="ck_leave_close_year"),
        CheckConstraint("length(trim(closed_by)) > 0", name="ck_leave_close_actor"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    month: Mapped[int] = mapped_column(Integer, nullable=False)
    backup_run_id: Mapped[int] = mapped_column(ForeignKey("backup_runs.id", ondelete="RESTRICT"), nullable=False)
    closed_by: Mapped[str] = mapped_column(String(80), nullable=False)
    closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Explicit policy snapshot/version for later previewed recalculation.
    policy_snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)


def _manual_opening_identity_check():
    # Portable equivalent of migration 0068's anchored lowercase-hex regex.
    # SQLite metadata fixtures must retain the same strict identity shape.
    def is_hex(expression):
        remainder = expression
        for character in "0123456789abcdef":
            remainder = f"replace({remainder}, '{character}', '')"
        return f"length({expression}) = 64 AND {remainder} = ''"
    return (
        "transaction_type = 'opening' AND length(source_operation_key) = 81 "
        "AND substr(source_operation_key, 1, 15) = 'manual_opening:' "
        "AND substr(source_operation_key, 80, 1) = ':' "
        "AND substr(source_operation_key, 81, 1) IN ('A','W','H') AND "
        + is_hex("substr(source_operation_key, 16, 64)") + " AND "
        + is_hex("source_digest")
        + " AND earning_year IS NULL AND released_on IS NULL"
    )


class LeaveBalanceTransaction(Base):
    __tablename__ = "leave_balance_transactions"
    __table_args__ = (
        UniqueConstraint("source_operation_key", name="uq_leave_ledger_source_operation"),
        CheckConstraint(
            "(source_operation_key IS NULL AND source_digest IS NULL AND earning_year IS NULL AND released_on IS NULL) OR "
            "(source_operation_key IS NOT NULL AND length(trim(source_operation_key)) > 0 "
            "AND source_digest IS NOT NULL AND length(source_digest) = 64 "
            "AND earning_year IS NOT NULL AND earning_year BETWEEN 1 AND 9999 AND released_on IS NOT NULL) OR "
            "COALESCE((" + _manual_opening_identity_check() + "), false)",
            name="ck_leave_ledger_source_identity"),
        CheckConstraint(
            "transaction_type IN ('opening', 'adjustment', 'leave_use', 'leave_reversal')",
            name="ck_leave_balance_transaction_type",
        ),
        CheckConstraint("days_delta <> 0", name="ck_leave_balance_transaction_nonzero"),
        CheckConstraint(
            "(days_delta IS NOT NULL AND exact_numerator IS NULL AND exact_denominator IS NULL) OR "
            "(days_delta IS NULL AND exact_numerator IS NOT NULL AND exact_denominator IS NOT NULL "
            "AND exact_numerator <> 0 AND exact_denominator > 1 "
            "AND exact_numerator = trunc(exact_numerator) AND exact_denominator = trunc(exact_denominator) "
            "AND length(exact_numerator::text) <= 32 AND length(exact_denominator::text) <= 32 "
            "AND exact_numerator::text NOT IN ('NaN','Infinity','-Infinity') "
            "AND exact_denominator::text NOT IN ('NaN','Infinity','-Infinity') "
            "AND gcd(exact_numerator, exact_denominator) = 1)",
            name="ck_leave_ledger_exact_amount").ddl_if(dialect="postgresql"),
        UniqueConstraint(
            "leave_record_id", "transaction_type",
            name="uq_leave_balance_transaction_record_type",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    leave_type_id: Mapped[int] = mapped_column(
        ForeignKey("leave_types.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    leave_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("leave_records.id", ondelete="RESTRICT"), index=True
    )
    transaction_type: Mapped[str] = mapped_column(String(24), nullable=False)
    days_delta: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    exact_numerator: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)
    exact_denominator: Mapped[Decimal | None] = mapped_column(Numeric(), nullable=True)

    @property
    def exact_days(self):
        from app.leave_exact_amount import ledger_amount
        return ledger_amount(self)

    @property
    def display_days(self):
        from app.leave_exact_amount import display_days
        return display_days(self.exact_days)

    source_operation_key: Mapped[str | None] = mapped_column(String(160))
    source_digest: Mapped[str | None] = mapped_column(String(64))
    earning_year: Mapped[int | None] = mapped_column(Integer)
    released_on: Mapped[date | None] = mapped_column(Date)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AdvanceRequest(Base):
    __tablename__ = "advance_requests"
    __table_args__ = (
        CheckConstraint("advance_type IN ('regular', 'installment')", name="ck_advance_requests_type"),
        CheckConstraint("amount > 0", name="ck_advance_requests_amount_positive"),
        CheckConstraint("salary_basis > 0", name="ck_advance_requests_salary_positive"),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'paid', 'deducted')",
            name="ck_advance_requests_status",
        ),
        Index("ix_advance_requests_payroll_run_id", "payroll_run_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    request_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    payroll_month: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    advance_type: Mapped[str] = mapped_column(String(20), default="regular", nullable=False)
    salary_basis: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_by: Mapped[str | None] = mapped_column(String(80))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[str | None] = mapped_column(String(80))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    paid_by: Mapped[str | None] = mapped_column(String(80))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deducted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payroll_run_id: Mapped[int | None] = mapped_column(ForeignKey("payroll_runs.id", ondelete="RESTRICT"))

class AdvanceInstallment(Base):
    """Immutable repayment schedule entry consumed by exactly one payroll run at a time."""
    __tablename__ = "advance_installments"
    __table_args__ = (
        UniqueConstraint("advance_request_id", "installment_number", name="uq_advance_installments_number"),
        UniqueConstraint("advance_request_id", "payroll_month", name="uq_advance_installments_payroll_month"),
        CheckConstraint("installment_number >= 1", name="ck_advance_installments_number"),
        CheckConstraint("amount > 0", name="ck_advance_installments_amount"),
        CheckConstraint("status IN ('scheduled', 'applied', 'reversed', 'cancelled')", name="ck_advance_installments_status"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    advance_request_id: Mapped[int] = mapped_column(ForeignKey("advance_requests.id", ondelete="RESTRICT"), nullable=False, index=True)
    installment_number: Mapped[int] = mapped_column(Integer, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    payroll_month: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    scheduled_payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="scheduled", nullable=False)
    applied_payroll_run_id: Mapped[int | None] = mapped_column(ForeignKey("payroll_runs.id", ondelete="RESTRICT"), index=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_reference: Mapped[str | None] = mapped_column(String(200))
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True,))

class StaffAction(Base):
    __tablename__ = "staff_actions"
    __table_args__ = (
        CheckConstraint("kind IN ('sanction', 'reward')", name="ck_staff_actions_kind"),
        CheckConstraint("amount IS NULL OR amount > 0", name="ck_staff_actions_amount_positive"),
        CheckConstraint(
            "status IN ('draft', 'approved', 'cancelled')",
            name="ck_staff_actions_status",
        ),
        CheckConstraint(
            "acknowledgement_status IN ('pending', 'viewed', 'declined')",
            name="ck_staff_actions_ack_status",
        ),
        CheckConstraint(
            "(is_financial = TRUE AND amount IS NOT NULL AND payroll_month IS NOT NULL) "
            "OR (is_financial = FALSE AND amount IS NULL AND payroll_month IS NULL)",
            name="ck_staff_actions_financial_fields",
        ),
        Index("ix_staff_actions_payroll_run_id", "payroll_run_id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    is_financial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    payroll_month: Mapped[date | None] = mapped_column(Date, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_by: Mapped[str | None] = mapped_column(String(80))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_by: Mapped[str | None] = mapped_column(String(80))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_reason: Mapped[str | None] = mapped_column(Text)
    acknowledgement_status: Mapped[str] = mapped_column(
        String(20), default="pending", nullable=False
    )
    acknowledgement_recorded_by: Mapped[str | None] = mapped_column(String(80))
    acknowledgement_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledgement_note: Mapped[str | None] = mapped_column(Text)
    payroll_applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payroll_run_id: Mapped[int | None] = mapped_column(ForeignKey("payroll_runs.id", ondelete="RESTRICT"))


class PayrollRun(Base):
    __tablename__ = "payroll_runs"
    __table_args__ = (
        UniqueConstraint("period_start", name="uq_payroll_runs_period_start"),
        CheckConstraint("period_end >= period_start", name="ck_payroll_runs_period"),
        CheckConstraint(
            "status IN ('draft', 'approved', 'closed', 'reopened')",
            name="ck_payroll_runs_status",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    pay_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", nullable=False)
    overtime_daily_required: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    approved_by: Mapped[str | None] = mapped_column(String(80))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[str | None] = mapped_column(String(80))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reopened_by: Mapped[str | None] = mapped_column(String(80))
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reopen_reason: Mapped[str | None] = mapped_column(Text)


class PayrollItem(Base):
    __tablename__ = "payroll_items"
    __table_args__ = (
        UniqueConstraint("payroll_run_id", "employee_id", name="uq_payroll_items_run_employee"),
        CheckConstraint("salary_basis >= 0", name="ck_payroll_items_salary_basis"),
        CheckConstraint("base_salary >= 0", name="ck_payroll_items_base_salary"),
        CheckConstraint("daily_hours > 0", name="ck_payroll_items_daily_hours"),
        CheckConstraint("overtime_hours >= 0", name="ck_payroll_items_overtime_hours"),
        CheckConstraint("absence_days >= 0", name="ck_payroll_items_absence_days"),
        CheckConstraint("late_minutes >= 0 AND early_minutes >= 0", name="ck_payroll_items_minutes"),
        CheckConstraint(
            "attendance_bonus >= 0 AND performance_bonus >= 0 AND occupancy_bonus >= 0 "
            "AND overtime_amount >= 0 AND other_bonus >= 0 AND absence_deduction >= 0 "
            "AND lateness_deduction >= 0 AND insurance_share >= 0 AND taxes >= 0 "
            "AND other_deductions >= 0 AND advance_deduction >= 0 "
            "AND staff_reward_amount >= 0 AND staff_sanction_amount >= 0 AND net_pay >= 0",
            name="ck_payroll_items_nonnegative_amounts",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    payroll_run_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    employee_id: Mapped[int] = mapped_column(
        ForeignKey("employees.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    employee_code_snapshot: Mapped[str] = mapped_column(String(20), nullable=False)
    employee_name_snapshot: Mapped[str] = mapped_column(String(200), nullable=False)
    department_name_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    position_snapshot: Mapped[str] = mapped_column(String(120), nullable=False)
    salary_basis: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    base_salary: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    daily_hours: Mapped[Decimal] = mapped_column(Numeric(6, 2), nullable=False)
    attendance_bonus: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    performance_bonus: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    occupancy_bonus: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    overtime_hours: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=0, nullable=False)
    overtime_approved: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    overtime_approved_by: Mapped[str | None] = mapped_column(String(80))
    overtime_approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    overtime_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    overtime_daily_digest: Mapped[str | None] = mapped_column(String(64))
    overtime_daily_hours: Mapped[str | None] = mapped_column(Text)
    other_bonus: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    absence_days: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=0, nullable=False)
    absence_deduction: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    late_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    early_minutes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    lateness_deduction: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    insurance_share: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    taxes: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    other_deductions: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    advance_deduction: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    staff_reward_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    staff_sanction_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    net_pay: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    absence_leave_reviewed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    absence_leave_review_fingerprint: Mapped[str | None] = mapped_column(String(64))
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class PayrollPayment(Base):
    __tablename__ = "payroll_payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="ck_payroll_payments_amount_positive"),
        CheckConstraint(
            "method IN ('cash', 'bank_transfer')",
            name="ck_payroll_payments_method",
        ),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    payroll_item_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_items.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(200))
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    superseded_by_payment_id: Mapped[int | None] = mapped_column(
        ForeignKey("payroll_payments.id", ondelete="RESTRICT")
    )
    correction_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("payroll_payments.id", ondelete="RESTRICT")
    )
    correction_reason: Mapped[str | None] = mapped_column(String(300))

class SystemSetting(Base):
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_by: Mapped[str] = mapped_column(String(80), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class BackupRun(Base):
    __tablename__ = "backup_runs"
    __table_args__ = (
        CheckConstraint(
            "backup_type IN ('daily', 'weekly', 'quarterly', 'manual', 'pre_restore')",
            name="ck_backup_runs_type",
        ),
        CheckConstraint(
            "status IN ('success', 'failed')",
            name="ck_backup_runs_status",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_backup_runs_size"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    backup_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    archive_path: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    initiated_by: Mapped[str] = mapped_column(String(80), nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)


class AICommandApproval(Base):
    __tablename__ = "ai_command_approvals"
    __table_args__ = (
        Index("ix_ai_command_approvals_status_expires_at", "status", "expires_at"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    actor: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    plan_json: Mapped[str] = mapped_column(Text, nullable=False)
    plan_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)


class AIConversationMessage(Base):
    __tablename__ = "ai_conversation_messages"
    __table_args__ = (
        Index("ix_ai_conversation_messages_actor_id", "actor", "id"),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    # Language the entry was generated in. Rows that predate this column are
    # "unknown": their origin language was never recorded, so nothing may
    # claim it. New writes pass the real UI language explicitly.
    language: Mapped[str] = mapped_column(String(8), nullable=False, server_default="unknown", default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class ReportPreset(Base):
    """A saved report configuration (type, period, scope, selected fields)."""

    __tablename__ = "report_presets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    config: Mapped[str] = mapped_column(Text, nullable=False)
    created_by: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LeaveRegisterReviewReceipt(Base):
    """Server-side short-lived reviewed preparation, consumed atomically with edit."""
    __tablename__ = "leave_register_review_receipts"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey("admin_accounts.id"), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False)
    proposal_json: Mapped[str] = mapped_column(Text, nullable=False)
    snapshot_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DepartmentWorkbookExport(Base):
    """Server-held identity/snapshot. Not a transferable authorization grant."""
    __tablename__ = 'department_workbook_exports'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_accounts.id'), nullable=False)
    manifest_json: Mapped[str] = mapped_column(Text, nullable=False)
    manifest_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    restore_epoch: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DepartmentWorkbookPreparation(Base):
    """Clean returned evidence receipt only. No accepted/posting state implied."""
    __tablename__ = 'department_workbook_preparations'
    __table_args__ = (UniqueConstraint('export_id', 'return_sha256', name='uq_dept_workbook_preparation'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    export_id: Mapped[str] = mapped_column(ForeignKey('department_workbook_exports.id'), nullable=False)
    return_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_accounts.id'), nullable=False)
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LeaveHistoricalEvidencePreparation(Base):
    """HR historical source proposal only. Never an accepted opening or grant."""
    __tablename__ = 'leave_historical_evidence_preparations'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_accounts.id'), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    proposal_json: Mapped[str] = mapped_column(Text, nullable=False)
    proposal_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    restore_epoch: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LeaveManualOpeningGroup(Base):
    """Explicit pending A/W/H selection, not accepted balances or a grant."""
    __tablename__ = 'leave_manual_opening_groups'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_accounts.id'), nullable=False)
    annual_id: Mapped[str] = mapped_column(ForeignKey('leave_historical_evidence_preparations.id'), nullable=False)
    weekly_id: Mapped[str] = mapped_column(ForeignKey('leave_historical_evidence_preparations.id'), nullable=False)
    holiday_id: Mapped[str] = mapped_column(ForeignKey('leave_historical_evidence_preparations.id'), nullable=False)
    selection_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    restore_epoch: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LeavePolicyVersion(Base):
    """Explicit immutable policy snapshot, never an automatic recalculation."""
    __tablename__ = 'leave_policy_versions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_accounts.id'), nullable=False)
    policy_json: Mapped[str] = mapped_column(Text, nullable=False)
    policy_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    restore_epoch: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LeaveOpeningSelection(Base):
    """Audited confirmed source selection, not a ledger opening or cash grant."""
    __tablename__ = 'leave_opening_selections'
    __table_args__ = (UniqueConstraint('employee_id','revision',name='uq_leave_opening_selection_revision'),)
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey('employees.id'), nullable=False)
    group_id: Mapped[str] = mapped_column(ForeignKey('leave_manual_opening_groups.id'), nullable=False)
    policy_id: Mapped[str] = mapped_column(ForeignKey('leave_policy_versions.id'), nullable=False)
    previous_id: Mapped[str | None] = mapped_column(ForeignKey('leave_opening_selections.id'))
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_accounts.id'), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    restore_epoch: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class DepartmentPreparationContract(Base):
    """Persist explicit confirmed export source inputs, not browser freshness."""
    __tablename__ = 'department_preparation_contracts'
    export_id: Mapped[str] = mapped_column(ForeignKey('department_workbook_exports.id'),primary_key=True)
    selection_json: Mapped[str] = mapped_column(Text,nullable=False)
    selection_sha256: Mapped[str] = mapped_column(String(64),nullable=False)


class DepartmentFingerprintPreparation(Base):
    """Original raw fingerprint and bounded comparison evidence, never registration."""
    __tablename__ = 'department_fingerprint_preparations'
    id: Mapped[str] = mapped_column(String(64),primary_key=True)
    return_id: Mapped[int] = mapped_column(ForeignKey('department_workbook_preparations.id'),nullable=False)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_accounts.id'),nullable=False)
    fingerprint_bytes: Mapped[bytes] = mapped_column(LargeBinary,nullable=False)
    fingerprint_sha256: Mapped[str] = mapped_column(String(64),nullable=False)
    review_json: Mapped[str] = mapped_column(Text,nullable=False)
    review_sha256: Mapped[str] = mapped_column(String(64),nullable=False)
    restore_epoch: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),nullable=False)


class LeavePolicyEditDraft(Base):
    """Explicit server-held settings preview, not active policy/recalculation."""
    __tablename__='leave_policy_edit_drafts'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    actor_id: Mapped[int]=mapped_column(ForeignKey('admin_accounts.id'),nullable=False)
    base_policy_id: Mapped[str]=mapped_column(ForeignKey('leave_policy_versions.id'),nullable=False)
    base_sha256: Mapped[str]=mapped_column(String(64),nullable=False)
    policy_json: Mapped[str]=mapped_column(Text,nullable=False)
    reason: Mapped[str]=mapped_column(Text,nullable=False)
    restore_epoch: Mapped[str | None]=mapped_column(Text)
    review_sha256: Mapped[str]=mapped_column(String(64),nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)
    saved_policy_id: Mapped[str | None]=mapped_column(ForeignKey('leave_policy_versions.id'))


class AttendanceEvidenceReviewReceipt(Base):
    """Pending source-bound review intent, not attendance/leave approval."""
    __tablename__='attendance_evidence_review_receipts'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    actor_id: Mapped[int]=mapped_column(ForeignKey('admin_accounts.id'),nullable=False)
    session_id: Mapped[str]=mapped_column(String(128),nullable=False)
    session_version: Mapped[int]=mapped_column(Integer,nullable=False)
    restore_epoch: Mapped[str | None]=mapped_column(Text)
    comparison_id: Mapped[str]=mapped_column(ForeignKey('department_fingerprint_preparations.id'),nullable=False)
    employee_id: Mapped[int]=mapped_column(ForeignKey('employees.id'),nullable=False)
    work_date: Mapped[date]=mapped_column(Date,nullable=False)
    proposal_json: Mapped[str]=mapped_column(Text,nullable=False)
    evidence_json: Mapped[str]=mapped_column(Text,nullable=False)
    source_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    request_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)


class AttendanceEvidenceConsumption(Base):
    """Immutable receipt consumption link; attendance remains pending review."""
    __tablename__='attendance_evidence_consumptions'
    receipt_id: Mapped[str]=mapped_column(ForeignKey('attendance_evidence_review_receipts.id'),primary_key=True)
    attendance_id: Mapped[int]=mapped_column(ForeignKey('attendance_daily_records.id'),nullable=False,unique=True)
    attendance_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)


class AttendanceEvidenceOccurrenceClaim(Base):
    """One original-byte occurrence cannot back two attendance registrations."""
    __tablename__='attendance_evidence_occurrence_claims'
    fingerprint_sha256: Mapped[str]=mapped_column(String(64),primary_key=True)
    ordinal: Mapped[int]=mapped_column(Integer,primary_key=True)
    receipt_id: Mapped[str]=mapped_column(ForeignKey('attendance_evidence_review_receipts.id'),nullable=False)


class LeaveSyntheticTransition(Base):
    """Local synthetic-only transition result, never a production authorization."""
    __tablename__ = 'leave_synthetic_transitions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_accounts.id'), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False)
    restore_epoch: Mapped[str | None] = mapped_column(Text)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    post_reconciliation_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LeaveSyntheticTransitionEntry(Base):
    __tablename__ = 'leave_synthetic_transition_entries'
    operation_id: Mapped[str] = mapped_column(ForeignKey('leave_synthetic_transitions.id'), primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey('employees.id'), primary_key=True)
    code: Mapped[str] = mapped_column(String(1), primary_key=True)
    ledger_id: Mapped[int | None] = mapped_column(ForeignKey('leave_balance_transactions.id'), unique=True)
    ledger_digest: Mapped[str | None] = mapped_column(String(64))


class LeaveSyntheticRegisterConsumption(Base):
    __tablename__='leave_synthetic_register_consumptions'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    comparison_id: Mapped[str]=mapped_column(ForeignKey('department_fingerprint_preparations.id'),nullable=False)
    employee_id: Mapped[int]=mapped_column(ForeignKey('employees.id'),nullable=False)
    actor_id: Mapped[int]=mapped_column(ForeignKey('admin_accounts.id'),nullable=False)
    session_id: Mapped[str]=mapped_column(String(128),nullable=False)
    session_version: Mapped[int]=mapped_column(Integer,nullable=False)
    restore_epoch: Mapped[str | None]=mapped_column(Text)
    source_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    result_json: Mapped[str]=mapped_column(Text,nullable=False)
    __table_args__=(UniqueConstraint('comparison_id','employee_id',name='uq_synthetic_register_comparison_employee'),)


class LeaveRegisterConsumption(Base):
    __tablename__='leave_register_consumptions'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    comparison_id: Mapped[str]=mapped_column(ForeignKey('department_fingerprint_preparations.id'),nullable=False)
    employee_id: Mapped[int]=mapped_column(ForeignKey('employees.id'),nullable=False)
    actor_id: Mapped[int]=mapped_column(ForeignKey('admin_accounts.id'),nullable=False)
    session_id: Mapped[str]=mapped_column(String(128),nullable=False)
    session_version: Mapped[int]=mapped_column(Integer,nullable=False)
    restore_epoch: Mapped[str | None]=mapped_column(Text)
    source_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    result_json: Mapped[str]=mapped_column(Text,nullable=False)
    __table_args__=(UniqueConstraint('comparison_id','employee_id',name='uq_register_comparison_employee'),)


class LeaveReconciliationReviewReceipt(Base):
    """Recorded operator assertions only; never verified facts or approval."""
    __tablename__='leave_reconciliation_review_receipts'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    comparison_id: Mapped[str]=mapped_column(ForeignKey('department_fingerprint_preparations.id'),nullable=False)
    employee_id: Mapped[int]=mapped_column(ForeignKey('employees.id'),nullable=False)
    actor_id: Mapped[int]=mapped_column(ForeignKey('admin_accounts.id'),nullable=False)
    session_id: Mapped[str]=mapped_column(String(128),nullable=False)
    session_version: Mapped[int]=mapped_column(Integer,nullable=False)
    restore_epoch: Mapped[str | None]=mapped_column(Text)
    source_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    decision_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    assertions_json: Mapped[str]=mapped_column(Text,nullable=False)
    reason: Mapped[str]=mapped_column(Text,nullable=False)
    integrity_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)


class LeaveReconciliationApproval(Base):
    """Immutable HR decision only, never a ledger or money instruction."""
    __tablename__='leave_reconciliation_approvals'
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    receipt_id: Mapped[str]=mapped_column(ForeignKey('leave_reconciliation_review_receipts.id'),nullable=False,unique=True)
    predecessor_id: Mapped[str | None]=mapped_column(ForeignKey('leave_reconciliation_approvals.id'),unique=True)
    scope_key: Mapped[str]=mapped_column(String(64),nullable=False,index=True)
    employee_id: Mapped[int]=mapped_column(ForeignKey('employees.id'),nullable=False)
    actor_id: Mapped[int]=mapped_column(ForeignKey('admin_accounts.id'),nullable=False)
    session_id: Mapped[str]=mapped_column(String(128),nullable=False)
    session_version: Mapped[int]=mapped_column(Integer,nullable=False)
    restore_epoch: Mapped[str | None]=mapped_column(Text)
    source_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    decision_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    comparison_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    request_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    reason: Mapped[str]=mapped_column(Text,nullable=False)
    integrity_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),nullable=False)
    __table_args__=(Index('uq_reconciliation_approval_baseline','scope_key',unique=True,postgresql_where=__import__('sqlalchemy').text('predecessor_id IS NULL')),)


class OvertimeDayReviewReceipt(Base):
    """Source-bound manual day review, no monthly payroll approval by itself."""
    __tablename__ = 'overtime_day_review_receipts'
    __table_args__ = (UniqueConstraint('id','item_id','work_date',name='uq_ot_receipt_scope'),
        CheckConstraint('expires_at > created_at',name='ck_ot_receipt_expiry'))
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey('payroll_items.id'), nullable=False)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_accounts.id'), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False)
    restore_epoch: Mapped[str | None] = mapped_column(Text)
    predecessor_id: Mapped[str | None] = mapped_column(ForeignKey('overtime_day_awards.receipt_id',use_alter=True,name='fk_ot_receipt_predecessor'))
    proposal_json: Mapped[str] = mapped_column(Text, nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    integrity_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class OvertimeDayAward(Base):
    """Immutable confirmed daily award, not a paid or monthly-posted receipt."""
    __tablename__ = 'overtime_day_awards'
    __table_args__ = (UniqueConstraint('predecessor_id', name='uq_overtime_award_successor'),
        ForeignKeyConstraint(['receipt_id','item_id','work_date'],['overtime_day_review_receipts.id','overtime_day_review_receipts.item_id','overtime_day_review_receipts.work_date'],name='fk_ot_award_receipt_scope'),
        Index('uq_overtime_award_baseline', 'item_id', 'work_date', unique=True, postgresql_where=__import__('sqlalchemy').text('predecessor_id IS NULL')),
        CheckConstraint('manual_hours >= 0 AND (manual_hours > 0 OR predecessor_id IS NOT NULL) AND rate_divisor IN (8,9,12)', name='ck_overtime_award_values'))
    receipt_id: Mapped[str] = mapped_column(ForeignKey('overtime_day_review_receipts.id'), primary_key=True)
    predecessor_id: Mapped[str | None] = mapped_column(ForeignKey('overtime_day_awards.receipt_id'))
    item_id: Mapped[int] = mapped_column(ForeignKey('payroll_items.id'), nullable=False)
    work_date: Mapped[date] = mapped_column(Date, nullable=False)
    manual_hours: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    rate_divisor: Mapped[int] = mapped_column(Integer, nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    amount_unrounded: Mapped[str] = mapped_column(Text, nullable=False)
    confirmed_by: Mapped[int] = mapped_column(ForeignKey('admin_accounts.id'), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class LeaveMonthEarning(Base):
    __tablename__ = 'leave_month_earnings'
    __table_args__ = (
        UniqueConstraint('employee_id','month_start',name='uq_leave_month_earning_scope'),
        CheckConstraint("strftime('%d',month_start) = '01'",name='ck_leave_month_earning_first').ddl_if(dialect='sqlite'),
        CheckConstraint('EXTRACT(DAY FROM month_start) = 1',name='ck_leave_month_earning_first').ddl_if(dialect='postgresql'),
    )
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey('employees.id',ondelete='RESTRICT'),nullable=False)
    selection_id: Mapped[str] = mapped_column(ForeignKey('leave_opening_selections.id',ondelete='RESTRICT'),nullable=False)
    month_start: Mapped[date] = mapped_column(Date,nullable=False)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_accounts.id',ondelete='RESTRICT'),nullable=False)
    session_id: Mapped[str] = mapped_column(String(128),nullable=False)
    session_version: Mapped[int] = mapped_column(Integer,nullable=False)
    restore_epoch: Mapped[str | None] = mapped_column(String(128))
    source_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    source_json: Mapped[str] = mapped_column(Text,nullable=False)
    result_json: Mapped[str] = mapped_column(Text,nullable=False)
    result_digest: Mapped[str] = mapped_column(String(64),nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)


class LeaveCarryAllocation(Base):
    """Exact source-year allocation, no new credit for an existing carry."""
    __tablename__='leave_carry_allocations'
    __table_args__=(UniqueConstraint('employee_id','leave_type_id','source_year',name='uq_leave_carry_source_year'),
        CheckConstraint('source_year BETWEEN 1 AND 9998',name='ck_leave_carry_scope'))
    id: Mapped[str]=mapped_column(String(64),primary_key=True)
    employee_id: Mapped[int]=mapped_column(ForeignKey('employees.id',ondelete='RESTRICT'),nullable=False)
    leave_type_id: Mapped[int]=mapped_column(ForeignKey('leave_types.id',ondelete='RESTRICT'),nullable=False)
    source_year: Mapped[int]=mapped_column(Integer,nullable=False)
    request_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    source_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    result_digest: Mapped[str]=mapped_column(String(64),nullable=False)
    request_json: Mapped[str]=mapped_column(Text,nullable=False)
    result_json: Mapped[str]=mapped_column(Text,nullable=False)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow,nullable=False)


class LeaveYearAttribution(Base):
    __tablename__ = 'leave_year_attributions'
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ledger_id: Mapped[int] = mapped_column(ForeignKey('leave_balance_transactions.id', ondelete='RESTRICT'), nullable=False, unique=True)
    actor_id: Mapped[int] = mapped_column(ForeignKey('admin_accounts.id', ondelete='RESTRICT'), nullable=False)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, nullable=False)
    restore_epoch: Mapped[str | None] = mapped_column(String(128))
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    result_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_json: Mapped[str] = mapped_column(Text, nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
