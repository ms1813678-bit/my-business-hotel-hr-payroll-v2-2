import pytest
from datetime import date
from app import weekly_summary as weekly
from app.national_id import is_valid_egyptian_national_id
from app.models import AuditLog
from app.leave_month_earning import review_month_earning
from tests.test_leave_month_earning import monthly  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_department_workbook_store import context  # noqa


def test_weekly_summary_counts_confirmed_leave_as_approved_label():
    # leave_records.status is constrained to draft/confirmed/cancelled; 'approved' never occurs.
    report = {"start": "2026-11-16", "end": "2026-11-22", "attendance_review": 0, "attendance_approved": 0,
              "leaves": {"draft": 1, "confirmed": 2, "cancelled": 3}, "advances": {}, "staff_actions": 0,
              "hires": 0, "latest_payroll": None}
    assert "2 approved" in weekly.format_weekly_summary(report, "en")
    assert "2 معتمدة" in weekly.format_weekly_summary(report, "ar")


@pytest.mark.parametrize('value', ['29' + '²' * 2 + '01010101234', '2\u0669' + '0' * 12, '2\u0662' + '01010101234' + '0'])
def test_national_id_non_ascii_digits_refuse_without_exception(value):
    assert is_valid_egyptian_national_id(value[:14].ljust(14, '0')) is False


@pytest.mark.parametrize('raw', ['not json', '[]', '"x"', '{"employee_id": null}', '{"employee_id": %d}',
    '{"employee_id": %d, "ledger": null}', '{"employee_id": %d, "ledger": [{}]}'])
def test_malformed_release_audit_is_a_controlled_refusal_never_ignored(monthly, raw):
    db, e, kinds, args = monthly
    if '%d' in raw: raw = raw % e.id
    db.add(AuditLog(actor='synthetic', action='leave_annual_released', entity_type='leave_annual_release',
                    entity_id='x', new_value=raw))
    db.flush()
    with pytest.raises(ValueError, match='Malformed annual release audit evidence'):
        review_month_earning(db, **args(date(2026, 2, 1), date(2026, 2, 28)))


def test_other_employee_wellformed_release_audit_does_not_block(monthly):
    db, e, kinds, args = monthly
    db.add(AuditLog(actor='synthetic', action='leave_annual_released', entity_type='leave_annual_release',
                    entity_id='x', new_value='{"employee_id": %d, "ledger": []}' % (e.id + 999)))
    db.flush()
    assert review_month_earning(db, **args(date(2026, 2, 1), date(2026, 2, 28)))['reviewed_digest']


from sqlalchemy import select as _select
from app.models import AdvanceRequest
from tests.test_advance_routes import advance_client, submit_advance, login as _login, csrf as _csrf  # noqa


def test_advance_submission_key_reuse_with_different_payload_is_refused(advance_client):  # noqa: F811
    client = advance_client
    _login(client)
    token = _csrf(client.get("/advances"))
    key = "a1b2c3d4" * 4
    first = submit_advance(client, token, amount="1000.00", advance_type="installment", submission_key=key)
    second = submit_advance(client, token, amount="2500.00", advance_type="installment", submission_key=key)
    assert first.status_code == 303 and second.status_code == 303
    assert "error" in second.headers["location"]
    with client.test_session_factory() as db:
        rows = db.scalars(_select(AdvanceRequest)).all()
        assert len(rows) == 1 and str(rows[0].amount) in ("1000.00", "1000")


def test_windows_postgres_tool_selection_prefers_highest_numeric_version(tmp_path, monkeypatch):
    import app.backup_workflow as bw
    from pathlib import Path
    root = tmp_path / 'PostgreSQL'
    for v in ('9.6', '10', '17', '16', 'notes'):
        (root / v / 'bin').mkdir(parents=True)
        (root / v / 'bin' / 'pg_dump.exe').write_text('x')
    monkeypatch.setattr(bw.shutil, 'which', lambda name: None)
    from types import SimpleNamespace
    monkeypatch.setattr(bw, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(bw, 'Path', lambda p: root)
    assert Path(bw._find_pg_tool('pg_dump')).parent.parent.name == '17'


@pytest.mark.parametrize('value', ['1E+999999', '1E-999999'])
def test_exact_adjustment_refuses_unbounded_exponent_before_review_or_storage(value):
    from decimal import Decimal
    from app.leave_exact_adjustment import ExactAdjustment, _validate
    with pytest.raises(ValueError, match='resource bound'):
        _validate(ExactAdjustment(1, 1, Decimal(value), 'r', 'k', 2026, date(2026, 1, 1)))


def test_ordinary_attendance_workbook_path_refuses_archive_expansion_bomb():
    import io, zipfile
    from app.attendance_import import _load_rows
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', '<Types/>')
        z.writestr('xl/worksheets/sheet1.xml', b'0' * 61_000_000)   # compresses to a few dozen KB
    assert len(buf.getvalue()) < 200_000
    with pytest.raises(ValueError, match='archive_limit'):
        _load_rows(buf.getvalue(), 'bomb.xlsx')


def test_missing_punch_flags_follow_payroll_precedence_including_approved_correction():
    from datetime import datetime
    from app.ai_hr_queries import _classify_attendance_row
    from app.models import AttendanceDailyRecord
    day = datetime(2026, 11, 2, 9, 0)
    only_approved = AttendanceDailyRecord(status='approved', approved_clock_in=day, approved_clock_out=day.replace(hour=17))
    codes = [n[0] for n in _classify_attendance_row(only_approved)]
    assert 'no_clock_in' not in codes and 'no_clock_out' not in codes
    nothing = AttendanceDailyRecord(status='needs_review')
    codes = [n[0] for n in _classify_attendance_row(nothing)]
    assert 'no_clock_in' in codes and 'no_clock_out' in codes


def _employee_session():
    from datetime import date as _d
    from decimal import Decimal as _D
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.db import Base
    from app.models import Department, Employee
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(bind=engine)
    db = sessionmaker(bind=engine)()
    dept = Department(name='D', active=True)
    db.add(dept); db.flush()
    emps = []
    for i, fp in enumerate(('0001', '0002')):
        e = Employee(code=f'T-{i}', full_name=f'E{i}', department_id=dept.id, position='p', hire_date=_d(2025, 1, 1),
                     status='active', basic_salary=_D('1000'), daily_hours=_D('8'), fingerprint_id=fp)
        db.add(e); emps.append(e)
    db.flush()
    return db, emps


def test_ai_edit_employee_refuses_duplicate_fingerprint_with_clean_error():
    from app import ai_agent
    db, (a, b) = _employee_session()
    with pytest.raises(ValueError):
        ai_agent.execute_plan({'action': 'edit_employee', 'employee_id': b.id, 'fingerprint_id': a.fingerprint_id}, 'a', db=db)
    assert b.fingerprint_id == '0002'


def test_security_roster_bad_date_returns_message_not_exception():
    from app.security_roster_compare import answer_security_roster_comparison

    for lang in ("ar", "en", "zh"):
        out = answer_security_roster_comparison("compare security 2026-13-45", language=lang)
        assert out and "Traceback" not in out
