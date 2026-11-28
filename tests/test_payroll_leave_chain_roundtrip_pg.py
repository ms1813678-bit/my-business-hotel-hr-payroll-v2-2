"""Alembic chain 0080 -> 0081 -> 0082 -> 0083: single head, full downgrade to 0080 and back up, columns checked at every step."""
import os, subprocess, sys
from pathlib import Path
from uuid import uuid4
from sqlalchemy import create_engine, text
from app.db import engine

ROOT = Path(__file__).resolve().parents[1]


def alembic(env, *a):
    return subprocess.run([sys.executable, '-m', 'alembic', *a], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def cols(c):
    return {r[0] for r in c.execute(text("SELECT column_name FROM information_schema.columns WHERE table_name='payroll_items'"))}


def table(c):
    return c.execute(text("SELECT count(*) FROM information_schema.tables WHERE table_name='leave_month_corrections'")).scalar_one()


def version(c):
    return c.execute(text('SELECT version_num FROM alembic_version')).scalar_one()


def test_chain_single_head_and_roundtrip_0083_0082_0081_0080():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    scripts = ScriptDirectory.from_config(Config(str(ROOT / 'alembic.ini')))
    assert scripts.get_heads() == ['0085_late_entry_months']
    assert scripts.get_revision('0085_late_entry_months').down_revision == '0084_sync_change_log'
    assert scripts.get_revision('0084_sync_change_log').down_revision == '0083_leave_month_correction'
    chain = ['0083_leave_month_correction', '0082_payroll_carry_deduction', '0081_payroll_leave_cash_line', '0080_restore_epoch_rebind']
    for child, parent in zip(chain, chain[1:]):
        assert scripts.get_revision(child).down_revision == parent
    name = 'test_chain_' + uuid4().hex[:10]
    admin = create_engine(engine.url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        url = engine.url.set(database=name)
        env = {**os.environ, 'DATABASE_URL': url.render_as_string(hide_password=False)}
        eng = create_engine(url)

        def step(cmd, rev, ver, leave_cash, carry, corr):
            r = alembic(env, cmd, rev); assert r.returncode == 0, r.stdout + r.stderr
            with eng.connect() as c:
                got = cols(c)
                assert version(c) == ver
                assert ('leave_cash_amount' in got) is leave_cash
                assert ({'carried_in_deduction', 'carried_out_amount'} <= got) is carry
                assert bool(table(c)) is corr
        step('upgrade', 'head', '0085_late_entry_months', True, True, True)
        step('downgrade', chain[1], chain[1], True, True, False)
        step('downgrade', chain[2], chain[2], True, False, False)
        step('downgrade', chain[3], chain[3], False, False, False)
        step('upgrade', 'head', '0085_late_entry_months', True, True, True)
        eng.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text('SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname=:n AND pid<>pg_backend_pid()'), dict(n=name))
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()
