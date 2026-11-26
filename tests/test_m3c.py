"""Batch 2 / item 3-C. NEW tests, source 0bc929d. Pin CURRENT behavior of the overtime
day-award schema (migration 0065 objects), correction-chain races and downgrade guards.
Reuses (does not edit) fixtures from tests/test_overtime_day_concurrency.py.
Rest-day semantics are NOT tested here (no rule exists); see the report.
"""
from datetime import date
from decimal import Decimal
import threading
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, select, text, func
from sqlalchemy.exc import DBAPIError
from alembic import command
from alembic.config import Config
from app.db import engine
from app.models import OvertimeDayAward
from app.overtime_day_award import prepare_day_award, confirm_day_award, current_awards, verified_day_totals
from tests.test_overtime_day_concurrency import committed_day  # noqa: F401

DAY = date(2026, 2, 2)


def prep(Session, auth, item_id, op, hours=2, reason='', pred=None):
    with Session.begin() as db:
        return prepare_day_award(db, operation_id=op * 64, item_id=item_id, work_date=DAY,
                                 manual_hours=Decimal(hours), rate_divisor=8, reason=reason,
                                 predecessor_id=pred, **auth).id


def confirm(Session, auth, receipt):
    with Session.begin() as db:
        return confirm_day_award(db, receipt_id=receipt, human_confirmed=True, **auth).receipt_id


def test_schema_objects_present_at_head(committed_day):
    Session, auth, item_id, att = committed_day
    with Session() as db:
        idx = {r[0]: r[1] for r in db.execute(text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname=current_schema() AND tablename='overtime_day_awards'"))}
        cons = {r[0] for r in db.execute(text(
            "SELECT conname FROM pg_constraint WHERE connamespace=current_schema()::regnamespace"))}
        trg = {r[0] for r in db.execute(text(
            "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal"))}
    assert 'predecessor_id IS NULL' in idx['uq_overtime_award_baseline']
    assert {'uq_overtime_award_successor', 'ck_overtime_award_values', 'fk_ot_award_receipt_scope',
            'uq_ot_receipt_scope', 'ck_ot_receipt_expiry', 'fk_ot_receipt_predecessor'} <= cons
    assert {'trg_overtime_day_awards_scope', 'trg_overtime_day_review_receipts_scope',
            'trg_overtime_day_awards_immutable', 'trg_overtime_day_review_receipts_immutable'} <= trg


def test_awards_and_receipts_are_update_delete_immutable(committed_day):
    Session, auth, item_id, att = committed_day
    r = prep(Session, auth, item_id, 'a'); confirm(Session, auth, r)
    for sql in ("UPDATE overtime_day_awards SET rate_divisor=9",
                "DELETE FROM overtime_day_awards",
                "UPDATE overtime_day_review_receipts SET session_id='x'",
                "DELETE FROM overtime_day_review_receipts"):
        with Session() as db:
            with pytest.raises(DBAPIError):
                db.execute(text(sql))
            db.rollback()
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(OvertimeDayAward)) == 1


@pytest.mark.parametrize('field,value', [('rate_divisor', 10), ('manual_hours', 0)])
def test_db_check_constraint_refuses_bad_award_values(committed_day, field, value):
    Session, auth, item_id, att = committed_day
    with Session() as db:
        db.execute(text("INSERT INTO overtime_day_review_receipts(id,item_id,work_date,actor_id,session_id,session_version,predecessor_id,proposal_json,source_digest,integrity_digest,created_at,expires_at) SELECT :n,:i,:d,a.id,'s',0,NULL,'{}',:z,:z,now(),now()+interval '5 minutes' FROM admin_accounts a LIMIT 1"),
                   dict(n='f' * 64, i=item_id, d=DAY, z='0' * 64))
        params = dict(n='f' * 64, i=item_id, d=DAY, h=Decimal(2), v=8, z='0' * 64)
        params[{'rate_divisor': 'v', 'manual_hours': 'h'}[field]] = value
        with pytest.raises(DBAPIError) as exc:
            db.execute(text("INSERT INTO overtime_day_awards(receipt_id,predecessor_id,item_id,work_date,manual_hours,rate_divisor,source_digest,amount_unrounded,confirmed_by,confirmed_at) SELECT :n,NULL,:i,:d,:h,:v,:z,'0',a.id,now() FROM admin_accounts a LIMIT 1"), params)
        assert 'ck_overtime_award_values' in str(exc.value)
        db.rollback()


def test_baseline_unique_per_item_day_at_db_level(committed_day):
    Session, auth, item_id, att = committed_day
    r = prep(Session, auth, item_id, 'a'); confirm(Session, auth, r)
    with Session() as db:
        db.execute(text("INSERT INTO overtime_day_review_receipts(id,item_id,work_date,actor_id,session_id,session_version,predecessor_id,proposal_json,source_digest,integrity_digest,created_at,expires_at) SELECT :n,item_id,work_date,actor_id,session_id,session_version,NULL,proposal_json,source_digest,integrity_digest,created_at,expires_at FROM overtime_day_review_receipts WHERE id=:o"),
                   dict(n='9' * 64, o=r))
        with pytest.raises(DBAPIError) as exc:
            db.execute(text("INSERT INTO overtime_day_awards(receipt_id,predecessor_id,item_id,work_date,manual_hours,rate_divisor,source_digest,amount_unrounded,confirmed_by,confirmed_at) SELECT :n,NULL,item_id,work_date,manual_hours,rate_divisor,source_digest,amount_unrounded,confirmed_by,confirmed_at FROM overtime_day_awards WHERE receipt_id=:o"),
                       dict(n='9' * 64, o=r))
        assert 'uq_overtime_award_baseline' in str(exc.value)
        db.rollback()


def test_two_corrections_of_same_award_race_one_wins(committed_day):
    Session, auth, item_id, att = committed_day
    base = prep(Session, auth, item_id, 'a'); confirm(Session, auth, base)
    b = prep(Session, auth, item_id, 'b', hours=1, reason='fix one', pred=base)
    c = prep(Session, auth, item_id, 'c', hours=Decimal('0.5'), reason='fix two', pred=base)
    barrier = threading.Barrier(2); out = []

    def run(rid):
        try:
            with Session.begin() as db:
                barrier.wait(timeout=5)
                out.append(('ok', confirm_day_award(db, receipt_id=rid, human_confirmed=True, **auth).receipt_id))
        except Exception as exc:  # noqa: BLE001
            out.append((type(exc).__name__, str(exc)))
    ws = [threading.Thread(target=run, args=(r,)) for r in (b, c)]
    [w.start() for w in ws]; [w.join(10) for w in ws]
    assert not any(w.is_alive() for w in ws) and len(out) == 2
    assert sorted(x[0] for x in out) == ['ValueError', 'ok']
    assert 'predecessor stale' in [x for x in out if x[0] == 'ValueError'][0][1]
    with Session() as db:
        assert db.scalar(select(func.count()).select_from(OvertimeDayAward)) == 2
        assert len(current_awards(db, item_id)) == 1


def test_correction_chain_to_zero_hours_head_totals(committed_day):
    Session, auth, item_id, att = committed_day
    a = prep(Session, auth, item_id, 'a'); confirm(Session, auth, a)
    b = prep(Session, auth, item_id, 'b', hours=1, reason='lower', pred=a); confirm(Session, auth, b)
    c = prep(Session, auth, item_id, 'c', hours=0, reason='withdraw', pred=b); confirm(Session, auth, c)
    with Session() as db:
        heads = current_awards(db, item_id)
        assert [h.receipt_id for h in heads] == [c]
        hours, amount, _ = verified_day_totals(db, item_id)
        assert hours == 0 and amount == 0
        # CURRENT: a new baseline for the day is still refused (history is append-only)
    with pytest.raises(ValueError, match='already awarded|stale'):
        prep(Session, auth, item_id, 'd')


def test_zero_hours_without_predecessor_refused_in_service(committed_day):
    Session, auth, item_id, att = committed_day
    with pytest.raises(ValueError, match='Zero hours'):
        prep(Session, auth, item_id, 'a', hours=0)


def test_correction_requires_reason_and_names_real_predecessor(committed_day):
    Session, auth, item_id, att = committed_day
    a = prep(Session, auth, item_id, 'a'); confirm(Session, auth, a)
    with pytest.raises(ValueError, match='correction reason'):
        prep(Session, auth, item_id, 'b', hours=1, reason='   ', pred=a)
    with pytest.raises(ValueError):
        prep(Session, auth, item_id, 'c', hours=1, reason='x', pred='e' * 64)


# ---- migration guard: downgrade refuses to round exact hours (owned schema, to 0065) ----
def _seed(conn, hours):
    conn.execute(text("INSERT INTO departments(id,name,active) VALUES(1,'Synthetic mig b2',true)"))
    conn.execute(text("INSERT INTO employees(id,code,full_name,department_id,position,hire_date,status,basic_salary,daily_hours) VALUES(1,'SYN-B2','Synthetic',1,'Synthetic','2025-01-01','active',6000,8)"))
    conn.execute(text("INSERT INTO payroll_runs(id,period_start,period_end,pay_date,status,created_by,created_at) VALUES(1,'2026-02-01','2026-02-28','2026-02-28','draft','synthetic',now())"))
    conn.execute(text("INSERT INTO payroll_items(id,payroll_run_id,employee_id,employee_code_snapshot,employee_name_snapshot,department_name_snapshot,position_snapshot,salary_basis,base_salary,daily_hours,overtime_hours,overtime_amount,net_pay,attendance_bonus,performance_bonus,occupancy_bonus,other_bonus,absence_days,absence_deduction,lateness_deduction,late_minutes,early_minutes,insurance_share,taxes,other_deductions,advance_deduction,staff_reward_amount,staff_sanction_amount,note,created_at,updated_at) VALUES(1,1,1,'SYN','S','S','S',6000,6000,8,:h,0,6000,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,'n',now(),now())"), dict(h=hours))


def test_0065_downgrade_refuses_exact_hours_then_allows_after_rounded_value():
    if engine.dialect.name != 'postgresql':
        pytest.skip('Migrated PostgreSQL required')
    schema = 'synthetic_ot_b2_' + uuid4().hex
    with engine.begin() as conn:
        conn.execute(text('CREATE SCHEMA ' + schema))
    local = create_engine(engine.url, connect_args={'options': '-csearch_path=' + schema})
    try:
        with local.begin() as conn:
            cfg = Config('alembic.ini'); cfg.attributes['connection'] = conn
            command.upgrade(cfg, '0064_reconciliation_approval')
            _seed(conn, 0)
            command.upgrade(cfg, '0065_overtime_day_review')
            assert conn.execute(text("SELECT numeric_scale FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='payroll_items' AND column_name='overtime_hours'")).scalar() == 6
            conn.execute(text("UPDATE payroll_items SET overtime_hours=3.125 WHERE id=1"))
            with pytest.raises(RuntimeError, match='Refusing downgrade: exact overtime hours'):
                command.downgrade(cfg, '0064_reconciliation_approval')
            conn.execute(text("UPDATE payroll_items SET overtime_hours=3.12 WHERE id=1"))
            command.downgrade(cfg, '0064_reconciliation_approval')
            assert conn.execute(text("SELECT numeric_scale FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='payroll_items' AND column_name='overtime_hours'")).scalar() == 2
            assert conn.execute(text('SELECT overtime_hours FROM payroll_items WHERE id=1')).scalar() == Decimal('3.12')
    finally:
        local.dispose()
        with engine.begin() as conn:
            conn.execute(text('DROP SCHEMA ' + schema + ' CASCADE'))
