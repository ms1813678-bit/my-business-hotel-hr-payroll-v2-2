"""Source-contract checks for complete migrated CI, not hosted execution proof."""
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/tests.yml'


def test_linux_ci_migrates_before_complete_suite():
    source = WORKFLOW.read_text()
    linux = source.split('  test:', 1)[1].split('  windows:', 1)[0]
    assert linux.index('python -m scripts.initialize_database') < linux.index('python -m scripts.run_ci_suite')
    assert 'postgres:16' in linux


def test_windows_ci_uses_real_isolated_database_and_complete_suite():
    source = WORKFLOW.read_text()
    windows = source.split('  windows:', 1)[1].split('  package-source:', 1)[0]
    assert 'ci_placeholder' not in windows
    assert '127.0.0.1:55432/hotel_test' in windows
    assert '--auth=scram-sha-256' in windows
    assert 'Refusing to reuse an existing CI cluster' in windows
    assert windows.index('initdb.exe') < windows.index('python -m scripts.initialize_database')
    assert windows.index('python -m scripts.initialize_database') < windows.index('python -m scripts.run_ci_suite')
    assert '--ignore' not in windows
    assert 'if: always()' in windows
    assert '-m fast -w stop' in windows
    assert 'GITHUB_PATH' in windows


def test_source_packaging_still_requires_both_verified_jobs():
    source = WORKFLOW.read_text().split('  package-source:', 1)[1]
    assert 'needs: [test, windows]' in source
    assert 'Refusing to package local/private data' in source
    assert 'actions/upload-artifact@v4' not in source


def test_complete_inventory_dispatch_no_missing_files():
    from scripts.run_ci_suite import assignments,CYCLE_FILES
    root=WORKFLOW.parents[2]
    plan=assignments(root)
    files=[f for _,group in plan for f in group]
    expected=sorted(p.relative_to(root).as_posix() for p in (root/'tests').rglob('test_*.py'))
    assert sorted(files)==expected and len(files)==len(set(files))
    assert plan[0][0].startswith('synthetic_leave_') and plan[1][0].startswith('synthetic_cycle_')
    assert set(plan[1][1])==CYCLE_FILES
    script=(root/'scripts/run_ci_suite.py').read_text()
    assert 'create_all' not in script and '--ignore' not in script and "'-k'" not in script
    assert "'CREATE DATABASE {}'" in script and "'DROP DATABASE {}'" in script
    assert 'if not files' in script and 'name already exists' in script


def test_cleanup_metadata_precedes_windows_start():
    windows=WORKFLOW.read_text().split('  windows:',1)[1].split('  package-source:',1)[0]
    assert windows.index('HONY_CI_PG_DATA=$cluster') < windows.index('-w start')
    assert 'permissions:\n  contents: read' in WORKFLOW.read_text()


def test_windows_unicode_database_not_os_ansi_default():
    assert '--encoding=UTF8 --locale=C' in WORKFLOW.read_text()


def test_dispatcher_migrates_and_runs_every_file_and_cleans(monkeypatch):
    from scripts import run_ci_suite as runner
    import psycopg
    calls=[];commands=[]
    class Cursor:
        def fetchone(self):return None
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def execute(self,query,params=None):calls.append((str(query),params));return Cursor()
    monkeypatch.setattr(psycopg,'connect',lambda *a,**k:Connection())
    monkeypatch.setenv('DATABASE_URL','postgresql+psycopg://synthetic@127.0.0.1/hotel_test')
    monkeypatch.setattr(runner.subprocess,'run',lambda args,**kw:commands.append((args,kw)))
    runner.main()
    assert len(commands)==4
    assert commands[0][0][-1]=='scripts.initialize_database' and commands[2][0][-1]=='scripts.initialize_database'
    assert 'synthetic_leave_ci' in commands[1][1]['env']['DATABASE_URL']
    assert 'synthetic_cycle_ci' in commands[3][1]['env']['DATABASE_URL']
    actual=commands[1][0][4:]+commands[3][0][4:]
    assert sorted(actual)==sorted(f for _,files in runner.assignments(WORKFLOW.parents[2]) for f in files)
    assert sum('CREATE DATABASE' in query for query,_ in calls)==2
    assert sum('DROP DATABASE' in query for query,_ in calls)==2


def test_dispatcher_failure_still_cleans_owned_databases(monkeypatch):
    from scripts import run_ci_suite as runner
    import psycopg,subprocess,pytest
    calls=[]
    class Cursor:
        def fetchone(self):return None
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def execute(self,query,params=None):calls.append(str(query));return Cursor()
    monkeypatch.setattr(psycopg,'connect',lambda *a,**k:Connection())
    monkeypatch.setenv('DATABASE_URL','postgresql+psycopg://synthetic@127.0.0.1/hotel_test')
    def failure(*a,**k):raise subprocess.CalledProcessError(1,a[0])
    monkeypatch.setattr(runner.subprocess,'run',failure)
    with pytest.raises(subprocess.CalledProcessError):runner.main()
    assert sum('DROP DATABASE' in query for query in calls)==2



def test_actual_initializer_uses_migration_not_schema_shortcut():
    root=WORKFLOW.parents[2]
    source=(root/'scripts/initialize_database.py').read_text()
    assert 'command.upgrade(config, "head")' in source
    assert 'create_all' not in source and '.stamp(' not in source


def test_nonloopback_and_query_override_refuse_before_connection(monkeypatch):
    from scripts import run_ci_suite as runner
    import psycopg,pytest
    def forbidden(*a,**k):raise AssertionError('must not connect')
    monkeypatch.setattr(psycopg,'connect',forbidden)
    for value in ('postgresql+psycopg://synthetic@remote.invalid/hotel_test',
                  'postgresql+psycopg://synthetic@127.0.0.1/hotel_test?host=remote.invalid',
                  'postgresql+psycopg://synthetic@127.0.0.1/hotel_test?service=external'):
        monkeypatch.setenv('DATABASE_URL',value)
        with pytest.raises(RuntimeError,match='loopback'):runner.main()


def test_collision_never_creates_or_drops_existing_db(monkeypatch):
    from scripts import run_ci_suite as runner
    import psycopg,pytest
    calls=[]
    class Cursor:
        def fetchone(self):return (1,)
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def execute(self,q,params=None):calls.append(str(q));return Cursor()
    monkeypatch.setattr(psycopg,'connect',lambda *a,**k:Connection())
    monkeypatch.setenv('DATABASE_URL','postgresql+psycopg://synthetic@127.0.0.1/hotel_test')
    with pytest.raises(RuntimeError,match='already exists'):runner.main()
    assert not any('CREATE DATABASE' in q or 'DROP DATABASE' in q for q in calls)


def test_empty_cycle_inventory_refuses(tmp_path):
    from scripts.run_ci_suite import assignments
    import pytest
    (tmp_path/'tests').mkdir()
    (tmp_path/'tests/test_only.py').write_text('')
    with pytest.raises(RuntimeError):assignments(tmp_path)


def test_drop_failure_preserves_primary_child_error(monkeypatch,capsys):
    from scripts import run_ci_suite as runner
    import psycopg,subprocess,pytest
    drops=[]
    class Cursor:
        def fetchone(self):return None
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def execute(self,q,params=None):
            if 'DROP DATABASE' in str(q):
                drops.append(str(q));raise RuntimeError('Synthetic cleanup failure')
            return Cursor()
    monkeypatch.setattr(psycopg,'connect',lambda *a,**k:Connection())
    monkeypatch.setenv('DATABASE_URL','postgresql+psycopg://synthetic@127.0.0.1/hotel_test')
    original=subprocess.CalledProcessError(7,['synthetic-child'])
    def fail(*a,**k):raise original
    monkeypatch.setattr(runner.subprocess,'run',fail)
    with pytest.raises(subprocess.CalledProcessError) as caught:runner.main()
    assert caught.value is original and len(drops)==2
    assert 'cleanup failed for owned databases' in capsys.readouterr().err


def test_drop_failure_after_success_is_not_silent(monkeypatch):
    from scripts import run_ci_suite as runner
    import psycopg,pytest
    class Cursor:
        def fetchone(self):return None
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def execute(self,q,params=None):
            if 'DROP DATABASE' in str(q):raise RuntimeError('Synthetic cleanup failure')
            return Cursor()
    monkeypatch.setattr(psycopg,'connect',lambda *a,**k:Connection())
    monkeypatch.setenv('DATABASE_URL','postgresql+psycopg://synthetic@127.0.0.1/hotel_test')
    monkeypatch.setattr(runner.subprocess,'run',lambda *a,**k:None)
    with pytest.raises(RuntimeError,match='cleanup failed'):runner.main()



def test_windows_cleanup_does_not_stop_never_started_cluster():
    source=WORKFLOW.read_text()
    cleanup=source.split('      - name: Stop isolated Windows PostgreSQL cluster',1)[1].split('  package-source:',1)[0]
    assert cleanup.index(' status') < cleanup.index('-m fast -w stop')
    assert '$status -eq 0' in cleanup and '$status -ne 3' in cleanup
    assert 'cluster retained for diagnosis' in cleanup
    assert cleanup.index('-m fast -w stop') < cleanup.index('Remove-Item')
    assert 'GITHUB_ENV' not in cleanup



def test_children_have_bounded_migration_and_suite_timeouts():
    source=(WORKFLOW.parents[2]/'scripts/run_ci_suite.py').read_text()
    assert 'check=True,timeout=120' in source
    assert 'check=True,timeout=900' in source
    assert WORKFLOW.read_text().count('timeout-minutes: 45')==3
    assert '[version]$_.Name' not in WORKFLOW.read_text()


def test_cleanup_connection_failure_preserves_primary(monkeypatch,capsys):
    from scripts import run_ci_suite as runner
    import psycopg,pytest,subprocess
    class Cursor:
        def fetchone(self):return None
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def execute(self,*a):return Cursor()
    count=0
    def connect(*a,**k):
        nonlocal count
        count+=1
        if count==2:raise RuntimeError('Synthetic cleanup connection failure')
        return Connection()
    monkeypatch.setattr(psycopg,'connect',connect)
    monkeypatch.setenv('DATABASE_URL','postgresql+psycopg://synthetic@127.0.0.1/hotel_test')
    failure=subprocess.TimeoutExpired('synthetic-child',1)
    def fail(*a,**k):raise failure
    monkeypatch.setattr(runner.subprocess,'run',fail)
    with pytest.raises(subprocess.TimeoutExpired) as caught:runner.main()
    assert caught.value is failure
    assert 'cleanup failed for owned databases' in capsys.readouterr().err



def test_windows_owned_leaf_validated_before_recursive_removal():
    source=WORKFLOW.read_text()
    start=source.split('      - name: Initialize isolated Windows PostgreSQL cluster',1)[1].split('      - name: Apply full Windows',1)[0]
    assert start.index('HONY_CI_PG_OWNER=$owner') < start.index('& "$pgBin\\initdb.exe"')
    cleanup=source.split('      - name: Stop isolated Windows PostgreSQL cluster',1)[1].split('  package-source:',1)[0]
    assert cleanup.index('$actual -ne $expected') < cleanup.index('Remove-Item')
    assert cleanup.index('CI cleanup ownership mismatch') < cleanup.index('Remove-Item')



def test_ci_has_no_hosted_artifact_or_dependency_cache_storage():
    source=WORKFLOW.read_text()
    assert 'actions/upload-artifact' not in source
    assert 'actions/cache' not in source
    assert 'cache: pip' not in source
    assert 'git archive --format=zip' in source
    assert 'Source archive hygiene check passed.' in source
