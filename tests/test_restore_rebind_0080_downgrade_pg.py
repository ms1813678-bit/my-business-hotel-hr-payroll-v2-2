"""0080 downgrade refusal with real duplicate proposal hashes (fresh scratch DB test_*)."""
import os, subprocess, sys
from pathlib import Path
from uuid import uuid4
import pytest
from sqlalchemy import create_engine, text
from app.db import engine

ROOT = Path(__file__).resolve().parents[1]
TBL = 'leave_historical_evidence_preparations'


def alembic(env, *a):
    return subprocess.run([sys.executable, '-m', 'alembic', *a], cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def snapshot(c):
    rows = c.execute(text(f'SELECT id,proposal_sha256,restore_epoch FROM {TBL} ORDER BY id')).all()
    idx = c.execute(text("SELECT indexname,indexdef FROM pg_indexes WHERE tablename=:t ORDER BY 1"), {'t': TBL}).all()
    cons = c.execute(text("SELECT conname FROM pg_constraint WHERE conrelid=CAST(:t AS regclass) ORDER BY 1"), {'t': TBL}).all()
    ver = c.execute(text('SELECT version_num FROM alembic_version')).all()
    return rows, idx, cons, ver


@pytest.mark.parametrize('epochs', [('E1', 'E2'), (None, 'E2')])
def test_downgrade_refuses_same_proposal_in_two_epochs_and_changes_nothing(epochs):
    name = 'test_0080_down_' + uuid4().hex[:10]
    admin = create_engine(engine.url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        url = engine.url.set(database=name)
        env = {**os.environ, 'DATABASE_URL': url.render_as_string(hide_password=False)}
        up = alembic(env, 'upgrade', '0080_restore_epoch_rebind'); assert up.returncode == 0, up.stdout + up.stderr
        # empty table: one-step downgrade and re-upgrade work (legacy compatible, no rows to retain)
        d0 = alembic(env, 'downgrade', '0079_leave_carry_expiry'); assert d0.returncode == 0, d0.stdout + d0.stderr
        u0 = alembic(env, 'upgrade', '0080_restore_epoch_rebind'); assert u0.returncode == 0, u0.stdout + u0.stderr
        eng = create_engine(url)
        with eng.begin() as c:
            c.execute(text("INSERT INTO admin_accounts (username,password_hash,role,active) VALUES ('mig-a','x','system_admin',true)"))
            actor = c.execute(text("SELECT id FROM admin_accounts WHERE username='mig-a'")).scalar_one()
            for i, ep in enumerate(epochs):
                c.execute(text(f"INSERT INTO {TBL} (id,actor_id,source_sha256,proposal_json,proposal_sha256,restore_epoch,created_at) "
                               "VALUES (:i,:a,:s,'{}',:p,:e,now())"), {'i': str(i) * 64, 'a': actor, 's': 'b' * 64, 'p': 'c' * 64, 'e': ep})
            # a distinct proposal in a single epoch must not itself block anything
            c.execute(text(f"INSERT INTO {TBL} (id,actor_id,source_sha256,proposal_json,proposal_sha256,restore_epoch,created_at) "
                           "VALUES (:i,:a,:s,'{}',:p,NULL,now())"), {'i': '7' * 64, 'a': actor, 's': 'b' * 64, 'p': 'd' * 64})
        with eng.connect() as c:
            before = snapshot(c)
        assert len(before[0]) == 3 and before[3] == [('0080_restore_epoch_rebind',)]
        bad = alembic(env, 'downgrade', '0079_leave_carry_expiry')
        assert bad.returncode != 0 and 'Refusing downgrade' in bad.stdout + bad.stderr, bad.stdout + bad.stderr
        with eng.connect() as c:
            after = snapshot(c)
        assert after == before                                   # rows, indexes, constraints, version all unchanged
        assert any(n == 'uq_leave_historical_proposal_epoch' for n, _ in after[1])
        assert not any('proposal_sha256_key' in n for (n,) in after[2])   # legacy unique was NOT re-added (no schema weakening or half-state)
        # remove the newer duplicate: downgrade now succeeds, retains one row per proposal, re-upgrade clean
        with eng.begin() as c:
            c.execute(text(f'DELETE FROM {TBL} WHERE id=:i'), {'i': '1' * 64})
        ok = alembic(env, 'downgrade', '0079_leave_carry_expiry'); assert ok.returncode == 0, ok.stdout + ok.stderr
        with eng.connect() as c:
            assert c.execute(text(f'SELECT count(*) FROM {TBL}')).scalar_one() == 2
            assert any('proposal_sha256_key' in n for (n,) in c.execute(text("SELECT conname FROM pg_constraint WHERE conrelid=CAST(:t AS regclass)"), {'t': TBL}).all())
        up2 = alembic(env, 'upgrade', '0080_restore_epoch_rebind'); assert up2.returncode == 0, up2.stdout + up2.stderr
        eng.dispose()
    finally:
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
