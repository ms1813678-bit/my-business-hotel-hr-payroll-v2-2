"""INNER cases for the duplicate-upload race. Run only through tests/test_attendance_upload_race_pg.py, which
gives every case its own disposable DB named test_upload_race_*. Refuses to run on any other database."""
import threading, time
from datetime import date, time as dtime
from io import BytesIO
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import func, select
from app import main
from app.db import SessionLocal, engine
from app.attendance_workflow import persist_attendance_import
from app.models import AdminAccount, AttendanceImportBatch, AttendanceMonth, AttendancePunch, AuditLog, Department, Employee
from decimal import Decimal

pytestmark = pytest.mark.skipif(not (engine.url.database or '').startswith('test_upload_race_'), reason='disposable DB required')
SOURCES = ['program_report', 'fingerprint']


@pytest.fixture(autouse=True)
def employee():
    assert (engine.url.database or '').startswith('test_upload_race_')
    with SessionLocal.begin() as db:
        d = Department(name='race-dep', active=True); db.add(d); db.flush()
        db.add(Employee(code='RACE-1', full_name='Race One', department_id=d.id, position='p', hire_date=date(2024, 1, 1),
                        status='active', basic_salary=Decimal(1), daily_hours=Decimal(8), fingerprint_id='RACE-1'))


def xlsx(source):
    w = Workbook(); s = w.active
    if source == 'program_report':
        s.append(["AC-No.", "Name", "Date", "Clock In", "Clock Out"]); s.append(["RACE-1", "Race One", date(2026, 11, 1), dtime(8, 0), dtime(16, 0)])
    else:
        s.append(["AC-No.", "Name", "Date", "Check Time"]); s.append(["RACE-1", "Race One", date(2026, 11, 1), "07:50 16:09"])
    o = BytesIO(); w.save(o); return o.getvalue()


def effects():
    with SessionLocal() as s:
        return tuple(s.scalar(select(func.count()).select_from(m)) for m in (AttendanceImportBatch, AttendanceMonth, AttendancePunch))


def parsed_for(raw, source):
    from app.attendance_import import parse_attendance_workbook
    with SessionLocal() as s:
        aliases = main._attendance_employee_aliases(s)
    return parse_attendance_workbook(raw, 'f.xlsx', source, aliases)


def err_info(e):
    orig = getattr(e, 'orig', None)
    return (type(e).__name__, getattr(orig, 'sqlstate', None), getattr(getattr(orig, 'diag', None), 'constraint_name', None), str(e)[:60])


@pytest.mark.parametrize('source', SOURCES)
def test_service_race_one_batch_clean_valueerror_and_callers_pending_work_kept(source):
    raw = xlsx(source); parsed = parsed_for(raw, source); out = {}; started = threading.Event(); b_pending_ready = threading.Event()
    def a():
        with SessionLocal() as s:
            out['a'] = persist_attendance_import(s, file_bytes=raw, filename='one.xlsx', parsed=parsed, actor='hr-a').id
            started.set(); b_pending_ready.wait(10); time.sleep(1.0); s.commit()
    def b():
        started.wait(10)
        with SessionLocal() as s:
            s.add(AuditLog(actor='hr-b', action='caller_pending_marker', entity_type='x', entity_id='1', reason='caller pre-existing pending work'))
            b_pending_ready.set()
            try:
                persist_attendance_import(s, file_bytes=raw, filename='renamed.xlsx', parsed=parsed, actor='hr-b'); out['b'] = 'ok'
            except Exception as e:  # noqa: BLE001
                out['b'] = err_info(e)
                if isinstance(e, ValueError):
                    s.commit()                     # caller keeps going: its own pending work must still be there
    ta, tb = threading.Thread(target=a), threading.Thread(target=b); ta.start(); tb.start(); ta.join(60); tb.join(60)
    assert not ta.is_alive() and not tb.is_alive()
    assert effects() == (1, 1, 2 if source == 'program_report' else 2), (effects(), out)  # 0 duplicate effect
    assert out['b'] != 'ok'
    # contract chosen by the writer: only the named unique violation (23505, uq_attendance_import_source_hash) -> ValueError
    assert isinstance(out['b'], tuple) and out['b'][0] == 'ValueError', f'BEFORE-FIX raw error: {out["b"]}'
    with SessionLocal() as s:
        assert s.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == 'caller_pending_marker')) == 1


@pytest.mark.parametrize('source', SOURCES)
def test_route_double_submit_loser_400_not_500(monkeypatch, source):
    raw = xlsx(source)
    fake = AdminAccount(username='race-route', password_hash='x', role='system_admin', active=True); fake.id = 1; fake.display_name = None
    main.app.dependency_overrides[main.require_attendance_write] = lambda: fake
    main.app.dependency_overrides[main.csrf_protect] = lambda: None
    monkeypatch.setattr(main, 'get_csrf_token', lambda request: 'x')
    first = threading.Event(); orig = main.persist_attendance_import
    def slow(*a, **k):
        r = orig(*a, **k)
        if not first.is_set():
            first.set(); time.sleep(1.5)
        return r
    monkeypatch.setattr(main, 'persist_attendance_import', slow)
    codes = {}
    try:
        with TestClient(main.app, raise_server_exceptions=False) as client:
            def post(tag, delay):
                time.sleep(delay)
                codes[tag] = client.post('/attendance/import', data={'source_type': source}, files={'file': ('f.xlsx', raw, 'application/octet-stream')}).status_code
            ta = threading.Thread(target=post, args=('a', 0)); tb = threading.Thread(target=post, args=('b', 0.5)); ta.start(); tb.start(); ta.join(60); tb.join(60)
    finally:
        main.app.dependency_overrides.clear()
    assert effects() == (1, 1, 2), (effects(), codes)
    assert codes == {'a': 200, 'b': 400}, f'BEFORE-FIX 500 means raw IntegrityError escaped: {codes}'
