"""Complete migrated CI suite across separately guarded synthetic databases.

No guard changes, selective deselection or hosted-readiness claim. Every test
file belongs to exactly one shard. Existing manual opt-in guards remain intact;
this dispatcher does not enable manual acceptance or certify skipped checks.
Only fresh fixed-name loopback databases are created. Existing DBs are refused.
"""
from pathlib import Path
import os
import subprocess
import sys

CYCLE_FILES=frozenset({'tests/test_return_register_cycle.py'})
# Measured wall seconds per file from the hosted Windows run of 8e1 (one 900s session
# of the whole leave shard timed out at 96%). Files >=8s only; others use the default.
# Weights only decide where contiguous chunk boundaries fall. Every file still runs
# exactly once, nothing is skipped, and the per-session cap stays 900 seconds.
WINDOWS_SECONDS_8E1={
    'tests/test_a1_department_asof.py':10,
    'tests/test_ai_atomic_corrections.py':22,
    'tests/test_ai_dispatch_http_pg.py':9,
    'tests/test_ai_error_http_pg.py':12,
    'tests/test_ai_pin.py':9,
    'tests/test_ai_plan_model_output.py':22,
    'tests/test_ai_selector_pg.py':22,
    'tests/test_attendance_evidence_review.py':28,
    'tests/test_backup_workflow.py':31,
    'tests/test_leave_exact_adjustment.py':16,
    'tests/test_leave_month_close_readiness.py':11,
    'tests/test_leave_month_earning.py':10,
    'tests/test_leave_opening_apply.py':24,
    'tests/test_leave_opening_apply_races.py':13,
    'tests/test_leave_opening_receipt_consumers.py':13,
    'tests/test_leave_register_preparation.py':21,
    'tests/test_leave_synthetic_transition.py':22,
    'tests/test_manual_opening_identity_migration.py':14,
    'tests/test_month_earning_http_pg.py':31,
    'tests/test_opening_apply_http_pg.py':16,
    'tests/test_overtime_day_award.py':10,
    'tests/test_overtime_day_concurrency.py':12,
    'tests/test_overtime_day_routes.py':15,
    'tests/test_payroll_guards.py':21,
    'tests/test_peer_manual_1003.py':11,
    'tests/test_postgres_migrations.py':11,
    'tests/test_reconciliation_approval.py':39,
    'tests/test_recovery.py':11,
    'tests/test_restore_cli_keyfile.py':9,
    'tests/test_return_register.py':24,
    'tests/test_return_register_http_pg.py':35,
    'tests/test_roles.py':22,
    'tests/test_routes_security.py':54,
}
DEFAULT_SECONDS=2
CHUNKS=3


def chunks(files,count=CHUNKS):
    """Contiguous, order-preserving split balanced by measured seconds."""
    files=list(files)
    weights=[WINDOWS_SECONDS_8E1.get(f,DEFAULT_SECONDS) for f in files]
    total=sum(weights)
    out=[[] for _ in range(count)];index=0;seen=0
    for f,weight in zip(files,weights):
        # Move to the next chunk once this chunk has its cumulative share.
        if index<count-1 and out[index] and seen+weight/2>total*(index+1)/count:
            index+=1
        out[index].append(f);seen+=weight
    if any(not group for group in out) or [f for group in out for f in group]!=files:
        raise RuntimeError('Every test file must run exactly once in a non-empty chunk.')
    return out


def assignments(root):
    files=sorted(p.relative_to(root).as_posix() for p in (root/'tests').rglob('test_*.py'))
    if not files or not CYCLE_FILES.issubset(files):
        raise RuntimeError('Complete known test inventory required.')
    cycle=[f for f in files if f in CYCLE_FILES]
    leave=[f for f in files if f not in CYCLE_FILES]
    if not leave or not cycle or set(leave)&set(cycle) or sorted(leave+cycle)!=files:
        raise RuntimeError('Every test file must run exactly once.')
    return [('synthetic_leave_ci',leave),('synthetic_cycle_ci',cycle)]


def main():
    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import make_url
    root=Path(__file__).resolve().parents[1]
    url=make_url(os.environ['DATABASE_URL'])
    if (url.drivername!='postgresql+psycopg' or url.host not in {'localhost','127.0.0.1','::1'}
            or url.query):
        raise RuntimeError('Explicit loopback PostgreSQL test source required.')
    plan=assignments(root)
    admin=url.set(database='postgres',drivername='postgresql').render_as_string(hide_password=False)
    # Refuse collisions before doing any work or dropping anything.
    created=[]
    failure=None
    suite_failures=[]
    try:
        with psycopg.connect(admin,autocommit=True) as db:
            for name,_ in plan:
                if db.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,)).fetchone():
                    raise RuntimeError('Fresh isolated CI databases required; name already exists.')
            for name,_ in plan:
                db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)));created.append(name)
        for name,files in plan:
            env=dict(os.environ,DATABASE_URL=url.set(database=name).render_as_string(hide_password=False))
            print(f'Full migrated CI shard {name}: {len(files)} test files',flush=True)
            subprocess.run([sys.executable,'-m','scripts.initialize_database'],cwd=root,env=env,check=True,timeout=120)
            groups=chunks(files) if len(files)>=CHUNKS*10 else [files]
            for number,group in enumerate(groups,1):
                print(f'  session {number}/{len(groups)}: {len(group)} test files',flush=True)
                try:
                    subprocess.run([sys.executable,'-m','pytest','-v',*group],cwd=root,env=env,check=True,timeout=900)
                except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as exc:
                    # A failing session must not silently leave later chunks unrun.
                    suite_failures.append((name,number,exc))
                    print(f'CI session failed: {name} {number}/{len(groups)} ({type(exc).__name__})',file=sys.stderr,flush=True)
        if suite_failures:
            raise RuntimeError('CI suite failed in sessions: '+', '.join(f'{name}:{number}' for name,number,_ in suite_failures))
    except BaseException as exc:
        failure=exc
        raise
    finally:
        # Drop only the DBs this exact invocation successfully created.
        cleanup_errors=[]
        if created:
            try:
                with psycopg.connect(admin,autocommit=True) as db:
                    for name in reversed(created):
                        try:
                            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
                        except Exception:
                            cleanup_errors.append(name)
            except Exception:
                cleanup_errors.extend(name for name in created if name not in cleanup_errors)
        if cleanup_errors:
            message='CI cleanup failed for owned databases: '+', '.join(cleanup_errors)
            if failure is not None:
                print(message,file=sys.stderr)
            else:
                raise RuntimeError(message)


if __name__=='__main__':main()
