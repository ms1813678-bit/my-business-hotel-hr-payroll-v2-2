"""One known Windows failure, same migrated CI source, no suite masking.

Diagnostic only. Uses a fresh uniquely named loopback DB and drops only its
own database in finally. An unexpected pass is still not domain acceptance.
"""
import os
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

NODE = 'tests/test_a1_department_asof.py::test_work_date_on_or_after_joining_current_department_is_accepted'


def main():
    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import make_url
    url = make_url(os.environ['DATABASE_URL'])
    if (url.drivername != 'postgresql+psycopg' or url.host not in {'localhost', '127.0.0.1', '::1'}
            or url.query):
        raise RuntimeError('Explicit loopback PostgreSQL test source required.')
    admin = url.set(database='postgres', drivername='postgresql').render_as_string(hide_password=False)
    name = 'synthetic_leave_diag_' + uuid4().hex[:12]
    created = False
    failure = None
    root = Path(__file__).resolve().parents[1]
    try:
        with psycopg.connect(admin, autocommit=True) as db:
            if db.execute('SELECT 1 FROM pg_database WHERE datname=%s', (name,)).fetchone():
                raise RuntimeError('Fresh diagnostic database required.')
            db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
            created = True
        env = dict(os.environ, DATABASE_URL=url.set(database=name).render_as_string(hide_password=False))
        subprocess.run([sys.executable, '-m', 'scripts.initialize_database'], cwd=root,
                       env=env, check=True, timeout=120)
        subprocess.run([sys.executable, '-m', 'pytest', NODE, '-x', '--tb=long', '-rf',
                        '-p', 'no:cacheprovider', '--durations=5'], cwd=root,
                       env=env, check=True, timeout=120)
        print('node_passed_in_isolation diagnostic_only=1 remaining_54_not_resolved=1', flush=True)
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if created:
            try:
                with psycopg.connect(admin, autocommit=True) as db:
                    db.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
            except Exception:
                if failure is None:
                    raise
                print('Diagnostic owned-database cleanup failed; original error retained.', file=sys.stderr)


if __name__ == '__main__':
    main()
