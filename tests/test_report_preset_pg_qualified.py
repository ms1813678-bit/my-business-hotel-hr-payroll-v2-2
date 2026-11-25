"""Local synthetic validation: real FastAPI app, real login + CSRF, real PostgreSQL (disposable), threads.
EXPECT=baseline|patched selects expected outcomes. Synthetic accounts only."""
import os, re, json, threading, time
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, delete

import app.main as main
from app.main import app
from app.db import engine, SessionLocal
from app.models import AdminAccount, ReportPreset
from app.security import hash_password
import app.report_builder as RB

EXPECT = os.environ.get("EXPECT", "patched")
pytestmark = pytest.mark.skipif(os.environ.get("PRESET_QUALIFICATION") != "1", reason="Dedicated disposable synth database qualification only")
@pytest.fixture(scope='module',autouse=True)
def disposable_database_only():
    # Opt-in is insufficient if pointed at the wrong database. Never mutate a
    # user's database: this retained qualification requires the owned local DB.
    if engine.dialect.name!='postgresql' or engine.url.host not in {'127.0.0.1','localhost'} or engine.url.database!='synth':
        pytest.fail('Preset qualification requires an owned loopback PostgreSQL database named synth.')

PW = "Synthetic-Pass-123!"
PAYROLL = {"report_type": "payroll", "fields": ["emp_name", "pay_net"], "period": "this_month"}
ATT = {"report_type": "attendance", "fields": ["emp_name", "emp_code"], "period": "this_month"}
ATT2 = {"report_type": "attendance", "fields": ["emp_code"], "period": "this_month"}


def csrf(r):
    m = re.search(r'name="csrf_token" value="([^"]+)"', r.text); assert m; return m.group(1)


def mk(username, role):
    with SessionLocal() as db:
        if db.scalar(select(AdminAccount.id).where(AdminAccount.username == username)) is None:
            db.add(AdminAccount(username=username, password_hash=hash_password(PW), display_name=username, role=role, active=True))
            db.commit()
    c = TestClient(app, follow_redirects=False)
    r = c.post("/login", data={"username": username, "password": PW, "csrf_token": csrf(c.get("/login"))})
    assert r.status_code == 303, r.status_code
    return c


@pytest.fixture(scope="module")
def clients():
    return {"hr": mk("syn-hr", "system_admin"), "fin": mk("syn-fin", "finance"), "asst": mk("syn-asst", "assistant_manager")}


@pytest.fixture(autouse=True)
def clean():
    with SessionLocal() as db:
        db.execute(delete(ReportPreset)); db.commit()
    yield


def form(cfg, name):
    d = {"preset_name": name, "report_type": cfg["report_type"], "period": cfg["period"], "fields": cfg["fields"]}
    return d


def post(c, cfg, name, token=None):
    page = c.get("/reports")
    d = form(cfg, name); d["csrf_token"] = csrf(page) if token is None else token
    return c.post("/reports/presets", data=d)


def row(name):
    with SessionLocal() as db:
        p = db.scalar(select(ReportPreset).where(ReportPreset.name == name))
        return (json.loads(p.config).get("report_type"), p.created_by) if p else None


def seed(name, cfg, by):
    with SessionLocal() as db:
        db.add(ReportPreset(name=name, config=json.dumps(cfg), created_by=by)); db.commit()


def test_isolation_default_is_read_committed():
    with SessionLocal() as db:
        assert db.execute(text("show transaction_isolation")).scalar() == "read committed"


def test_no_session_and_bad_csrf(clients):
    seed("N", PAYROLL, "syn-hr")
    anon = TestClient(app, follow_redirects=False)
    r = anon.post("/reports/presets", data={**form(ATT, "N"), "csrf_token": "x"})
    assert r.status_code in (401, 403, 303)
    assert row("N") == ("payroll", "syn-hr")
    c = clients["asst"]
    assert c.post("/reports/presets", data={**form(ATT, "N"), "csrf_token": "wrong"}).status_code == 403
    assert row("N") == ("payroll", "syn-hr")


def test_assistant_overwrites_payroll_preset_defect(clients):
    seed("N", PAYROLL, "syn-hr")
    r = post(clients["asst"], ATT, "N")
    if EXPECT == "baseline":
        assert r.status_code == 303 and row("N") == ("attendance", "syn-asst")   # defect reproduced
    else:
        assert r.status_code == 403 and row("N") == ("payroll", "syn-hr")


def test_in_scope_shared_overwrite_and_hr_overwrite(clients):
    seed("S", ATT, "syn-hr")
    assert post(clients["asst"], ATT2, "S").status_code == 303 and row("S") == ("attendance", "syn-asst")
    assert post(clients["hr"], PAYROLL, "S").status_code == 303 and row("S") == ("payroll", "syn-hr")


def test_new_config_still_scope_checked(clients):
    assert post(clients["asst"], PAYROLL, "X").status_code == 403 and row("X") is None


def test_finance_vs_admin_only_type(clients):
    seed("C", {"report_type": "comprehensive", "fields": [], "period": "this_month"}, "syn-hr")
    r = post(clients["fin"], ATT, "C")
    if EXPECT == "baseline":
        assert r.status_code == 303      # same defect for finance
    else:
        assert r.status_code == 403 and row("C")[0] == "comprehensive"


def test_name_validation_gives_error_redirect(clients):
    r = post(clients["hr"], ATT, "n" * 121)
    assert r.status_code == 303 and "error=preset" in r.headers["location"]


def delete_req(c, pid):
    return c.post(f"/reports/presets/{pid}/delete", data={"csrf_token": csrf(c.get("/reports"))})


def pid(name):
    with SessionLocal() as db:
        return db.scalar(select(ReportPreset.id).where(ReportPreset.name == name))


def test_delete_http(clients):
    seed("P", PAYROLL, "syn-hr"); seed("A", ATT, "syn-hr")
    assert delete_req(clients["asst"], pid("P")).status_code == 403 and row("P")
    assert delete_req(clients["asst"], pid("A")).status_code == 303 and row("A") is None
    assert delete_req(clients["asst"], 999999).status_code == 303
    assert delete_req(clients["hr"], pid("P")).status_code == 303 and row("P") is None


def wait_blocked(timeout=6):
    end = time.time() + timeout
    while time.time() < end:
        with engine.connect() as c:
            n = c.execute(text("select count(*) from pg_stat_activity where wait_event_type='Lock' and datname='synth' and pid<>pg_backend_pid()")).scalar()
        if n:
            return True
        time.sleep(0.05)
    return False


def hr_flip_uncommitted(name):
    """HR transaction: lock row, flip to payroll, leave uncommitted. returns (conn, trans)."""
    conn = engine.connect(); tx = conn.begin()
    conn.execute(text("select id from report_presets where name=:n for update"), {"n": name})
    conn.execute(text("update report_presets set config=:c, created_by='syn-hr' where name=:n"),
                 {"c": json.dumps(PAYROLL), "n": name})
    return conn, tx


def test_race_check_vs_write_existing_row(clients):
    seed("R", ATT, "syn-hr")
    conn, tx = hr_flip_uncommitted("R")
    out = {}
    t = threading.Thread(target=lambda: out.update(r=post(clients["asst"], ATT2, "R"), at=time.time()))
    t.start()
    blocked = wait_blocked()
    committed_at = time.time(); tx.commit(); conn.close()
    t.join(15)
    assert not t.is_alive()
    r = out["r"]
    print("EXPECT", EXPECT, "blocked_on_lock", blocked, "status", r.status_code, "row", row("R"))
    if EXPECT == "baseline":
        assert r.status_code == 303 and row("R") == ("attendance", "syn-asst")   # payroll clobbered
    else:
        assert blocked and out["at"] >= committed_at
        assert r.status_code == 403 and row("R") == ("payroll", "syn-hr")


def test_race_in_scope_flip_proceeds(clients):
    if EXPECT == "baseline": pytest.skip("baseline has no lock")
    seed("R2", ATT, "syn-hr")
    conn = engine.connect(); tx = conn.begin()
    conn.execute(text("select id from report_presets where name='R2' for update"))
    conn.execute(text("update report_presets set config=:c where name='R2'"), {"c": json.dumps(ATT)})
    out = {}
    t = threading.Thread(target=lambda: out.update(r=post(clients["asst"], ATT2, "R2")))
    t.start(); assert wait_blocked(); tx.commit(); conn.close(); t.join(15)
    assert not t.is_alive()
    assert out["r"].status_code == 303 and row("R2") == ("attendance", "syn-asst")


def test_race_delete_vs_flip(clients):
    seed("D", ATT, "syn-hr")
    conn, tx = hr_flip_uncommitted("D")
    out = {}
    t = threading.Thread(target=lambda: out.update(r=delete_req(clients["asst"], pid("D"))))
    t.start(); blocked = wait_blocked(); tx.commit(); conn.close(); t.join(15)
    assert not t.is_alive()
    print("EXPECT", EXPECT, "blocked", blocked, "status", out["r"].status_code, "row", row("D"))
    if EXPECT == "baseline":
        # baseline: check read ATT before the flip? (check happens before blocking DELETE) -> payroll preset deleted
        assert row("D") is None
    else:
        assert out["r"].status_code == 403 and row("D") == ("payroll", "syn-hr")


@pytest.mark.parametrize("hr_first", [True, False])
def test_race_create_collision(clients, hr_first):
    if EXPECT == "baseline": pytest.skip("baseline lacks helper")
    barrier = threading.Barrier(2, timeout=8)
    from sqlalchemy.orm import Session
    orig = Session.scalar
    seen = threading.local()

    def hooked(self, stmt, *a, **k):
        res = orig(self, stmt, *a, **k)
        if "report_presets" in str(stmt) and "FOR UPDATE" in str(stmt.compile(dialect=engine.dialect)) and not getattr(seen, "done", False):
            seen.done = True
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
        return res
    Session.scalar = hooked
    out = {}
    try:
        def go(k, c, cfg, delay):
            time.sleep(delay)
            out[k] = post(c, cfg, "NEWRACE")
        ts = [threading.Thread(target=go, args=("hr", clients["hr"], PAYROLL, 0 if hr_first else 0.0)),
              threading.Thread(target=go, args=("asst", clients["asst"], ATT, 0))]
        [x.start() for x in ts]; [x.join(20) for x in ts]
        assert not any(t.is_alive() for t in ts)
    finally:
        Session.scalar = orig
    print("collision", {k: v.status_code for k, v in out.items()}, row("NEWRACE"))
    codes = {k: v.status_code for k, v in out.items()}
    assert all(c in (303, 403) for c in codes.values())     # never 500
    with SessionLocal() as db:
        assert db.scalar(text("select count(*) from report_presets where name='NEWRACE'")) == 1
    cfgtype, by = row("NEWRACE")
    # asst may never leave a payroll config; if HR won, asst must have 403'd
    assert not (cfgtype == "payroll" and by == "syn-asst")
    if codes["asst"] == 403:
        assert cfgtype == "payroll" and by == "syn-hr"   # asst refused only because HR payroll row was there
    # if asst won, HR (allowed to replace) must have replaced it or lost order: both end 303
    assert codes["hr"] == 303


def test_outer_transaction_usable_after_integrityerror():
    if EXPECT == "baseline": pytest.skip()
    seed("SP", ATT, "syn-hr")
    with SessionLocal() as db:
        from sqlalchemy.orm import Session
        orig = Session.scalar
        calls = {"n": 0}
        def fake(self, stmt, *a, **k):
            if "FOR UPDATE" in str(stmt.compile(dialect=engine.dialect)):
                calls["n"] += 1
                if calls["n"] == 1:
                    return None      # force the insert path against an existing name
            return orig(self, stmt, *a, **k)
        Session.scalar = fake
        try:
            p = RB.save_preset(db, "SP", ATT2, "syn-asst", existing_check=lambda c: None)
            db.commit()
        finally:
            Session.scalar = orig
    assert calls["n"] == 2 and row("SP") == ("attendance", "syn-asst")


def test_lock_released_after_403(clients):
    if EXPECT == "baseline": pytest.skip()
    seed("L", PAYROLL, "syn-hr")
    assert post(clients["asst"], ATT, "L").status_code == 403
    t0 = time.time()
    assert post(clients["hr"], PAYROLL, "L").status_code == 303
    assert time.time() - t0 < 3
    with engine.connect() as c:
        assert c.execute(text("select count(*) from pg_stat_activity where datname='synth' and state like 'idle in transaction%'")).scalar() == 0


def test_deadlock_probe_opposite_order(clients):
    if EXPECT == "baseline": pytest.skip()
    seed("O1", ATT, "syn-hr"); seed("O2", ATT, "syn-hr")
    errs = []
    def w(c, names):
        for _ in range(15):
            for n in names:
                r = post(c, ATT2, n)
                if r.status_code not in (303,): errs.append(r.status_code)
    a = threading.Thread(target=w, args=(clients["asst"], ["O1", "O2"]))
    b = threading.Thread(target=w, args=(clients["hr"], ["O2", "O1"]))
    a.start(); b.start(); a.join(60); b.join(60)
    assert not a.is_alive() and not b.is_alive()
    assert not errs


def test_mixed_stress_same_name_no_500(clients):
    if EXPECT == "baseline": pytest.skip()
    errs = []; codes = []
    def w(c, cfg):
        for _ in range(10):
            r = post(c, cfg, "STRESS"); codes.append(r.status_code)
    ts = [threading.Thread(target=w, args=(clients["hr"], PAYROLL)) for _ in range(3)] + \
         [threading.Thread(target=w, args=(clients["asst"], ATT)) for _ in range(3)]
    [t.start() for t in ts]; [t.join(120) for t in ts]
    assert not any(t.is_alive() for t in ts)
    assert set(codes) <= {303, 403}, set(codes)
    assert row("STRESS") is not None
    with SessionLocal() as db:
        assert db.scalar(text("select count(*) from report_presets where name='STRESS'")) == 1
