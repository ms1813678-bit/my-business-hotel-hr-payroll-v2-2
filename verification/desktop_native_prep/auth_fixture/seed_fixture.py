#!/usr/bin/env python3
"""Synthetic seed helper for authenticated desktop-flow fixture (request 0559,
repair of 0554).

UNVERIFIED against the real repo. Uses the app's own models, password hasher and
the REAL payroll creation path: app.payroll_workflow.create_payroll_run followed
by app.payroll_workflow.update_payroll_item (the same functions the POST
/payroll and POST /payroll/items/<id>/update routes exercise; see
tests/test_payroll_guards.py:create_run/set_absence). It never inserts
PayrollRun/PayrollItem rows directly. Does NOT import app.main (no server, no
startup hooks). Never prints credentials.

Prerequisite fixture seeding (plain inserts, same shape as the guard_client
fixture in tests/test_payroll_guards.py:37-103): department, employee + service
period, fixture admin account, two APPROVED AttendanceImportBatch rows and one
APPROVED AttendanceMonth for the target month, one confirmed LeaveRecord on the
MIGRATION-SEEDED paid leave type `annual` (seeded by alembic
0016_leave_management; the helper refuses if that row is absent).

Seeded attendance approval is NOT exercised approval: no import, reconciliation
or approve_attendance_month flow ran; the seeded rows only satisfy the
create_payroll_run precondition (payroll_workflow.py:270-289). The seed writes
no audit rows for prerequisites; the only audit rows after seeding are the real
ones written by create_payroll_run (payroll_run_created) and
update_payroll_item (payroll_item_updated).

Migration-head matching is done through the actual migration framework
(alembic ScriptDirectory over <source-root>/alembic), not a hardcoded revision:
the helper computes the single head and requires the DB's alembic_version to
equal it. Migration-seeded default rows are honored, not assumed empty:
leave_types (6 rows from 0016) and employee_code_counters (row id=1 from 0002)
must be present; a fresh migrated DB is still required (no admin/employee/
payroll/attendance/audit rows).

DB effects (all in the disposable DB you point it at, one transaction):
  departments +1, employees +1, employee_service_periods +1, admin_accounts +1,
  attendance_import_batches +2, attendance_months +1, leave_records +1,
  payroll_runs +1 (via create_payroll_run), payroll_items +1 (via
  create_payroll_run), audit_logs +2 (payroll_run_created, payroll_item_updated).
  leave_types +0 and employee_code_counters +0 (migration-seeded, reused).
File effects: none. Network: none (psycopg to loopback only, enforced).

Credentials: username via --username, password via env FIXTURE_ADMIN_PASSWORD
or --password-file (>= 12 chars). Never on argv, never printed.
"""
import argparse, json, os, secrets, sys
from datetime import date
from decimal import Decimal
from pathlib import Path

DB_NAME_MARKER = "fixture"   # DB name must contain this; refuses anything else

# Migration-seeded leave types that MUST exist on a fully migrated fresh DB
# (alembic/versions/0016_leave_management.py:110-121).
EXPECTED_SEED_LEAVE_TYPES = {
    "annual": True, "sick": True, "casual": True,
    "official_holiday": True, "weekly_rest": True, "unpaid": False,
}
FIXTURE_LEAVE_TYPE_CODE = "annual"  # paid; used for the conflict-trigger leave

# Tables that must be row-empty on a fresh migrated DB for the seed to run.
FRESH_EMPTY_TABLES = (
    "admin_accounts", "employees", "departments", "branches",
    "payroll_runs", "payroll_items", "payroll_payments",
    "leave_records", "attendance_import_batches", "attendance_months",
    "attendance_daily_records", "attendance_punches", "admin_sessions",
    "audit_logs", "advance_requests", "staff_actions", "employee_changes",
)


def _fail(msg):
    print(json.dumps({"ok": False, "error": msg}), file=sys.stderr)
    sys.exit(2)


def _migration_head(source_root):
    """Single alembic head computed through the actual migration framework."""
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    cfg = Config()
    cfg.set_main_option("script_location", str(Path(source_root) / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    if len(heads) != 1:
        _fail(f"alembic tree has {len(heads)} heads ({heads}); expected exactly one")
    return heads[0]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--source-root", required=True, help="dir containing app/ and alembic/")
    ap.add_argument("--database-url", default=os.environ.get("FIXTURE_DATABASE_URL"),
                    help="postgresql+psycopg://user:pw@127.0.0.1/<name containing 'fixture'>")
    ap.add_argument("--username", required=True)
    ap.add_argument("--password-file")
    ap.add_argument("--start-date", required=True, help="YYYY-MM-DD, synthetic")
    ap.add_argument("--end-date", required=True, help="YYYY-MM-DD, same calendar month as start")
    a = ap.parse_args(argv)

    # --- stdlib-only validation first (so refusal paths run without app deps) ---
    start, end = date.fromisoformat(a.start_date), date.fromisoformat(a.end_date)
    if end < start or (start.year, start.month) != (end.year, end.month):
        _fail("range must be within ONE calendar month (payroll run = that whole month)")
    if not a.database_url:
        _fail("database url required")
    password = Path(a.password_file).read_text().strip() if a.password_file else os.environ.get("FIXTURE_ADMIN_PASSWORD", "")
    if len(password) < 12:
        _fail("password missing or < 12 chars")
    # Integrations must be off: refuse if env could enable them.
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "LOGIN_ALERT_WEBHOOK_URL"):
        if os.environ.get(var):
            _fail(f"{var} is set; unset it for the fixture")

    os.environ["DATABASE_URL"] = a.database_url
    os.environ["SESSION_SECRET"] = os.environ.get("SESSION_SECRET") or secrets.token_urlsafe(48)
    os.environ["LOGIN_ALERT_WEBHOOK_URL"] = ""
    sys.path.insert(0, a.source_root)

    from sqlalchemy import select, text
    from sqlalchemy.engine import make_url
    from app.config import settings          # validates loopback + secret
    dbname = make_url(settings.database_url).database or ""
    if DB_NAME_MARKER not in dbname.lower():
        _fail(f"database name must contain '{DB_NAME_MARKER}'")
    if settings.login_alert_webhook_url.strip():
        _fail("login alert webhook not empty")
    from app.db import SessionLocal, engine
    from app import models as m
    from app.security import hash_password
    from app.payroll_workflow import (
        create_payroll_run, payroll_period, update_payroll_item,
    )

    # --- migration head matched through the actual migration framework ---
    expected_head = _migration_head(a.source_root)
    with engine.connect() as c:
        rev = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        if not rev:
            _fail("alembic_version table missing or empty: not a migrated database")
        if rev != expected_head:
            _fail(f"DB at alembic revision {rev!r}, framework head is {expected_head!r}: "
                  "run scripts.initialize_database (alembic upgrade head) first")
        for trigger, table in (("trg_audit_logs_immutable", "audit_logs"),
                               ("trg_leave_source_immutable", "leave_balance_transactions")):
            n = c.execute(text(
                "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
                "WHERE t.tgname = :tg AND c.relname = :tbl AND t.tgenabled = 'O'"),
                {"tg": trigger, "tbl": table}).scalar()
            if not n:
                _fail(f"expected trigger {trigger} on {table} missing: DB does not match migrated head")

    with SessionLocal.begin() as db:
        # --- freshness: no business rows, migration-seeded rows present ---
        for table in FRESH_EMPTY_TABLES:
            count = db.scalar(text(f"SELECT count(*) FROM {table}"))
            if count:
                _fail(f"{table} has {count} rows: refusing, need a FRESH migrated DB")
        seed_types = {code: paid for code, paid in
                      db.execute(text("SELECT code, is_paid FROM leave_types"))}
        if seed_types != EXPECTED_SEED_LEAVE_TYPES:
            _fail(f"leave_types {sorted(seed_types)} != migration-seeded set "
                  f"{sorted(EXPECTED_SEED_LEAVE_TYPES)} (0016_leave_management)")
        counter = db.execute(text("SELECT last_number FROM employee_code_counters WHERE id = 1")).scalar()
        if counter is None:
            _fail("employee_code_counters id=1 missing (seeded by 0002_platform_state)")
        if counter != 0:
            _fail("employee_code_counters.last_number != 0: DB is not fresh")
        if db.get(m.SystemSetting, "telegram_alerts") is not None:
            _fail("telegram_alerts setting present")

        run_start, run_end, _pay_date = payroll_period(start.year, start.month)
        hire = date(start.year - 1, start.month, 1)
        actor = a.username.strip()

        # --- prerequisite fixture seeding (mirrors tests/test_payroll_guards.py
        # guard_client lines 37-103; seeded approval is NOT exercised approval) ---
        dept = m.Department(name="Synthetic Fixture Dept", active=True)
        db.add(dept); db.flush()
        emp = m.Employee(code="SYN-0001", full_name="Synthetic Fixture Employee",
                         nationality="egyptian", department_id=dept.id, position="Synthetic Tester",
                         hire_date=hire, status="active",
                         basic_salary=Decimal("6000.00"), daily_hours=Decimal("8.00"))
        db.add(emp); db.flush()
        db.add(m.EmployeeServicePeriod(employee_id=emp.id, start_date=hire, end_date=None,
                                       start_reason="Synthetic fixture employment"))
        admin = m.AdminAccount(username=actor, password_hash=hash_password(password),
                               display_name="Fixture Admin", role="system_admin", active=True,
                               must_change_password=False, pending_credential_reset=False)
        db.add(admin)
        program = m.AttendanceImportBatch(
            source_type="program_report", period_start=run_start, period_end=run_end,
            original_filename="synthetic-program.xlsx", file_sha256="a" * 64,
            uploaded_by=actor, status="approved", row_count=1, issue_count=0)
        fingerprint = m.AttendanceImportBatch(
            source_type="fingerprint", period_start=run_start, period_end=run_end,
            original_filename="synthetic-fingerprint.xlsx", file_sha256="b" * 64,
            uploaded_by=actor, status="approved", row_count=1, issue_count=0)
        db.add_all([program, fingerprint]); db.flush()
        month = m.AttendanceMonth(
            period_start=run_start, period_end=run_end,
            program_import_batch_id=program.id, fingerprint_import_batch_id=fingerprint.id,
            status="approved", created_by=actor, approved_by=actor,
            approved_at=m.utcnow(),
            approval_reason="Synthetic approved attendance (seeded fixture; approval flow NOT exercised)")
        db.add(month); db.flush()
        leave_type = db.scalar(select(m.LeaveType).where(m.LeaveType.code == FIXTURE_LEAVE_TYPE_CODE))
        if leave_type is None or not leave_type.is_paid:
            _fail(f"migration-seeded paid leave type {FIXTURE_LEAVE_TYPE_CODE!r} not found")
        leave = m.LeaveRecord(employee_id=emp.id, leave_type_id=leave_type.id,
                              start_date=run_start, end_date=run_start, calendar_days=1,
                              charged_days=Decimal("1"), reason="Synthetic fixture leave",
                              status="confirmed", created_by=actor,
                              confirmed_by=actor, confirmed_at=m.utcnow())
        db.add(leave); db.flush()

        # --- REAL business path: no direct PayrollRun/PayrollItem insertion ---
        run = create_payroll_run(db, year=start.year, month=start.month, actor=actor)
        item = db.scalar(select(m.PayrollItem).where(m.PayrollItem.payroll_run_id == run.id))
        if item is None or item.employee_id != emp.id:
            _fail("create_payroll_run did not produce exactly the fixture employee item")
        # Same full manual-field set the UI's update form submits (tests
        # test_payroll_guards.py:set_absence), with absence_days = 1 so the
        # absence/confirmed-paid-leave conflict (and its review form) exists.
        update_payroll_item(db, item, {
            "attendance_bonus": "0", "performance_bonus": "0", "occupancy_bonus": "0",
            "overtime_hours": "0", "other_bonus": "0", "absence_days": "1",
            "lateness_deduction": "0", "insurance_share": "0", "taxes": "0",
            "other_deductions": "0", "note": "",
        }, actor=actor)
        db.flush()

        out = {"ok": True,
               "admin_account_id": admin.id, "employee_id": emp.id, "department_id": dept.id,
               "attendance_month_id": month.id,
               "program_import_batch_id": program.id, "fingerprint_import_batch_id": fingerprint.id,
               "leave_record_id": leave.id, "leave_type_code": leave_type.code,
               "payroll_run_id": run.id, "payroll_item_id": item.id,
               "period_start": run_start.isoformat(), "period_end": run_end.isoformat(),
               "report_start_date": start.isoformat(), "report_end_date": end.isoformat(),
               "alembic_head": expected_head,
               "payroll_creation_path": "app.payroll_workflow.create_payroll_run + update_payroll_item (real business path; no direct PayrollRun/PayrollItem insert)",
               "attendance_approval": "seeded prerequisite, NOT exercised",
               "leave_confirmation": "seeded prerequisite, NOT exercised",
               "expected_row_counts_added": {
                   "departments": 1, "employees": 1, "employee_service_periods": 1,
                   "admin_accounts": 1, "attendance_import_batches": 2,
                   "attendance_months": 1, "leave_types": 0, "leave_records": 1,
                   "employee_code_counters": 0,
                   "payroll_runs": 1, "payroll_items": 1, "audit_logs": 2},
               "expected_after_seed": {
                   "payroll_runs.status": "draft",
                   "payroll_items.absence_days": "1.00",
                   "payroll_items.absence_leave_reviewed": False,
                   "admin_sessions": 0, "payroll_payments": 0,
                   "audit_logs_actions": ["payroll_item_updated", "payroll_run_created"]}}
    print(json.dumps(out, sort_keys=True))


if __name__ == "__main__":
    main()
