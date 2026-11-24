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
            subprocess.run([sys.executable,'-m','pytest','-v',*files],cwd=root,env=env,check=True,timeout=900)
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
