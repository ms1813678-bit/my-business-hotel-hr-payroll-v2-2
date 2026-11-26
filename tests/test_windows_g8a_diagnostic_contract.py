"""Diagnostic orchestration only, no Windows or PDF acceptance claim."""
from pathlib import Path
import importlib.util
import subprocess
import pytest

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'scripts/windows_first_attendance_failure.py'


def load():
    spec=importlib.util.spec_from_file_location('g8a_diag',SCRIPT)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_fixed_scope_and_unchanged_windows_fixture_workflow():
    module=load()
    assert module.NODE=='tests/test_g8a_monthly_boundary_oracle.py::test_lower_edge_non_predecessor_sections_render'
    workflow=(ROOT/'.github/workflows/windows-attendance-first-failure.yml').read_text()
    assert 'timeout-minutes: 12' in workflow and 'runs-on: windows-latest' in workflow
    paths=workflow.split('    paths:',1)[1].split('  workflow_dispatch:',1)[0]
    assert paths.count('      - ')==2
    assert '.github/workflows/windows-attendance-first-failure.yml' in paths
    assert 'scripts/windows_first_attendance_failure.py' in paths
    assert 'matrix:' not in workflow and 'native' not in workflow and 'run_ci_suite' not in workflow
    assert 'python -m scripts.windows_first_attendance_failure' in workflow


@pytest.mark.parametrize('outcome',['pass','failure','timeout'])
def test_one_node_migrated_owned_database_cleanup_and_original_outcome(monkeypatch,capsys,outcome):
    import psycopg
    module=load();queries=[];commands=[]
    class Cursor:
        def fetchone(self):return None
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def execute(self,q,params=None):queries.append((str(q),params));return Cursor()
    monkeypatch.setattr(psycopg,'connect',lambda *a,**k:Connection())
    monkeypatch.setenv('DATABASE_URL','postgresql+psycopg://synthetic@127.0.0.1/hotel_test')
    failure=subprocess.CalledProcessError(1,['synthetic']) if outcome=='failure' else subprocess.TimeoutExpired(['synthetic'],120)
    def run(args,**kw):
        commands.append((args,kw))
        if args[2:3]==['pytest'] and outcome!='pass':raise failure
    monkeypatch.setattr(module.subprocess,'run',run)
    if outcome=='pass':
        module.main();assert 'adapter_acceptance=NOT_ESTABLISHED hotel_acceptance=NOT_ESTABLISHED' in capsys.readouterr().out
    else:
        with pytest.raises(type(failure)) as caught:module.main()
        assert caught.value is failure
    assert len(commands)==2 and commands[0][0][-1]=='scripts.initialize_database'
    assert commands[1][0][2:4]==['pytest',module.NODE]
    assert '--tb=long' in commands[1][0] and '-x' in commands[1][0]
    assert all(kw['timeout']==120 and kw['check'] is True for _,kw in commands)
    assert sum('CREATE DATABASE' in q for q,_ in queries)==1
    assert sum('DROP DATABASE' in q for q,_ in queries)==1
    assert 'synthetic_leave_diag_' in commands[1][1]['env']['DATABASE_URL']


def test_existing_database_collision_never_runs_or_drops(monkeypatch):
    import psycopg
    module=load();queries=[]
    class Cursor:
        def fetchone(self):return (1,)
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*_):pass
        def execute(self,q,params=None):queries.append(str(q));return Cursor()
    monkeypatch.setattr(psycopg,'connect',lambda *a,**k:Connection())
    monkeypatch.setenv('DATABASE_URL','postgresql+psycopg://synthetic@127.0.0.1/hotel_test')
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k:pytest.fail('collision must not run'))
    with pytest.raises(RuntimeError,match='Fresh diagnostic'):module.main()
    assert not any('CREATE DATABASE' in q or 'DROP DATABASE' in q for q in queries)


def test_fixed_function_selects_exactly_seven_parameters():
    import ast
    tree=ast.parse((ROOT/'tests/test_g8a_monthly_boundary_oracle.py').read_text())
    node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='test_lower_edge_non_predecessor_sections_render')
    decorators=[d for d in node.decorator_list if isinstance(d,ast.Call)]
    assert len(decorators)==1 and ast.literal_eval(decorators[0].args[0])=='options'
    assert len(ast.literal_eval(decorators[0].args[1]))==7
