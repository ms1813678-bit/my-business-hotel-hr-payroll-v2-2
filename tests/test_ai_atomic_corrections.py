"""Synthetic regression for exact review and atomic local AI execution."""
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.ai_agent as agent
import app.ai_gateway as gateway
from app.ai_plan_validation import validate_plan_values
from app.db import Base
from app.models import AICommandApproval, AuditLog, Department, Employee, PayrollItem, PayrollRun


@pytest.fixture(params=['sqlite', 'postgresql'])
def synthetic(monkeypatch, request):
    schema = None
    if request.param == 'postgresql':
        from app.db import engine as host
        schema = 'synthetic_ai_' + uuid4().hex
        with host.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(host.url, connect_args={'options': f'-csearch_path={schema}'})
        from tests.test_leave_precision_migration import migrate
        with engine.begin() as connection:
            migrate(connection, 'head')
    else:
        engine = create_engine('sqlite://', poolclass=StaticPool)
        Base.metadata.create_all(engine)
    factory = sessionmaker(engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(gateway, 'SessionLocal', factory)
    monkeypatch.setattr(agent, 'SessionLocal', factory)
    with factory.begin() as db:
        department = Department(name='Synthetic AI', active=True)
        db.add(department)
        db.flush()
        employee = Employee(code='SYN-AI', full_name='Synthetic only',
            department_id=department.id, position='Synthetic', hire_date=date(2026,1,1),
            basic_salary=Decimal(1000), daily_hours=Decimal(8), status='active')
        run = PayrollRun(period_start=date(2026,1,1), period_end=date(2026,1,31),
                        pay_date=date(2026,2,5), status='draft', created_by='synthetic')
        db.add_all([employee,run])
        db.flush()
        db.add(PayrollItem(payroll_run_id=run.id, employee_id=employee.id,
            employee_code_snapshot='SYN-AI',employee_name_snapshot='Synthetic',
            department_name_snapshot='Synthetic',position_snapshot='Synthetic',
            salary_basis=Decimal(1000),base_salary=Decimal(1000),daily_hours=Decimal(8)))
    yield factory
    engine.dispose()
    if schema:
        with host.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))


def proposal(factory, plan):
    token = uuid4().hex
    plan['_confirmation_snapshot'] = gateway.build_confirmation_preview(plan)['snapshot']
    with factory.begin() as db:
        db.add(AICommandApproval(token_hash=gateway._token_hash(token), actor='synthetic',
            question='Synthetic review', plan_json=gateway._canonical_json(plan),
            plan_hash=gateway.plan_hash(plan), status='pending', created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC)+timedelta(minutes=10)))
    return token


@pytest.mark.parametrize('value', ['false', 'true', 0, 1, [], {}])
@pytest.mark.parametrize('key', ['coverage_confirmed', 'is_financial'])
def test_model_boolean_coercions_rejected_before_preview(value, key):
    with pytest.raises(ValueError, match='Invalid command values'):
        validate_plan_values({'action':'approve_attendance_month',key:value}, 'en')


@pytest.mark.parametrize('value', ['NaN','Infinity','-Infinity',True])
def test_nonfinite_financial_inputs_rejected(value):
    with pytest.raises(ValueError):
        validate_plan_values({'action':'create_advance','amount':value})


def test_changed_payroll_line_invalidates_review(synthetic,monkeypatch):
    token=proposal(synthetic,{'action':'approve_payroll_run','payroll_run_id':1})
    with synthetic.begin() as db:
        db.get(PayrollItem,1).performance_bonus=Decimal(500)
    monkeypatch.setattr(gateway,'execute_plan',lambda *args,**kwargs: pytest.fail('stale execution'))
    with pytest.raises(ValueError,match='تغيرت'):
        gateway.approve_command(token,'synthetic')
    with synthetic() as db:
        assert db.scalar(select(AICommandApproval)).status=='invalidated'
        assert db.get(PayrollRun,1).status=='draft'


def test_execute_failure_rolls_back_domain_and_token_together(synthetic,monkeypatch):
    token=proposal(synthetic,{'action':'approve_payroll_run','payroll_run_id':1})
    def fail(plan,actor,*,db,**kwargs):
        db.get(PayrollRun,1).status='approved'
        db.flush()
        raise RuntimeError('Synthetic failure after domain flush')
    monkeypatch.setattr(gateway,'execute_plan',fail)
    with pytest.raises(RuntimeError,match='Synthetic failure'):
        gateway.approve_command(token,'synthetic')
    with synthetic() as db:
        assert db.get(PayrollRun,1).status=='draft'
        assert db.scalar(select(AICommandApproval)).status=='pending'
        assert db.scalar(select(AuditLog.id).where(AuditLog.action=='ai_command_executed')) is None


def test_success_commits_domain_token_audit_and_history_once(synthetic,monkeypatch):
    token=proposal(synthetic,{'action':'approve_payroll_run','payroll_run_id':1})
    def success(plan,actor,*,db,**kwargs):
        db.get(PayrollRun,1).status='approved'
        return agent.ExecutionResult('approve_payroll_run',{'run_id':1})
    monkeypatch.setattr(gateway,'execute_plan',success)
    gateway.approve_command(token,'synthetic')
    with synthetic() as db:
        assert db.get(PayrollRun,1).status=='approved'
        assert db.scalar(select(AICommandApproval)).status=='consumed'
        assert db.scalar(select(AuditLog.id).where(AuditLog.action=='ai_command_executed')) is not None
    with pytest.raises(ValueError,match='سبق'):
        gateway.approve_command(token,'synthetic')
    assert len(gateway.recent_memory('synthetic'))==1


def test_model_true_never_counts_as_human_attestation(monkeypatch):
    monkeypatch.setattr(agent,'approve_attendance_month',lambda db,**kwargs:
        (_ for _ in ()).throw(ValueError('coverage required')) if not kwargs['coverage_confirmed'] else None)
    with pytest.raises(ValueError,match='coverage required'):
        agent.execute_plan({'action':'approve_attendance_month','month_id':1,
                            'coverage_confirmed':True},'synthetic')


def test_postgresql_table_lock_blocks_competing_direct_item_writer(synthetic):
    if synthetic.kw['bind'].dialect.name != 'postgresql':
        pytest.skip('PostgreSQL lock semantics only')
    from sqlalchemy.exc import OperationalError
    with synthetic.begin() as first:
        gateway._lock_command_business_tables(first)
        with synthetic.begin() as second:
            second.execute(text("SET LOCAL lock_timeout = '150ms'"))
            with pytest.raises(OperationalError, match='lock timeout'):
                second.execute(text('UPDATE payroll_items SET performance_bonus=900 WHERE id=1'))
    with synthetic() as db:
        assert db.get(PayrollItem,1).performance_bonus == 0


def test_postgresql_duplicate_approval_executes_once(synthetic,monkeypatch):
    if synthetic.kw['bind'].dialect.name != 'postgresql':
        pytest.skip('PostgreSQL concurrent transaction semantics only')
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    entered, release = Event(), Event()
    token = proposal(synthetic, {'action':'approve_payroll_run','payroll_run_id':1})
    calls = []
    def executor(plan,actor,*,db,**kwargs):
        calls.append(1)
        db.get(PayrollRun,1).status='approved'
        entered.set()
        assert release.wait(2)
        return agent.ExecutionResult('approve_payroll_run',{'run_id':1})
    monkeypatch.setattr(gateway,'execute_plan',executor)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(gateway.approve_command, token, 'synthetic')
        assert entered.wait(2)
        second = pool.submit(gateway.approve_command, token, 'synthetic')
        release.set()
        first.result(timeout=4)
        with pytest.raises(ValueError,match='سبق'):
            second.result(timeout=4)
    assert calls == [1]
    with synthetic() as db:
        assert db.scalar(select(AICommandApproval)).status=='consumed'


def test_interrupt_before_commit_rolls_back_business_and_consumption(synthetic,monkeypatch):
    from sqlalchemy import event
    token=proposal(synthetic,{'action':'approve_payroll_run','payroll_run_id':1})
    def executor(plan,actor,*,db,**kwargs):
        db.get(PayrollRun,1).status='approved'
        db.flush()
        def interrupt(connection):
            raise RuntimeError('Synthetic commit interruption')
        event.listen(db.connection(),'commit',interrupt,once=True)
        return agent.ExecutionResult('approve_payroll_run',{'run_id':1})
    monkeypatch.setattr(gateway,'execute_plan',executor)
    with pytest.raises(RuntimeError,match='commit interruption'):
        gateway.approve_command(token,'synthetic')
    with synthetic() as db:
        assert db.get(PayrollRun,1).status=='draft'
        assert db.scalar(select(AICommandApproval)).status=='pending'


def test_postgresql_process_death_before_commit_leaves_no_partial_approval(synthetic,monkeypatch,tmp_path):
    if synthetic.kw['bind'].dialect.name != 'postgresql':
        pytest.skip('Real PostgreSQL process-death recovery only')
    import os
    import subprocess
    import sys
    token = proposal(synthetic,{'action':'approve_payroll_run','payroll_run_id':1})
    engine = synthetic.kw['bind']
    # Child connects only to this disposable synthetic schema. No credentials
    # or private data are embedded in its source; config comes from test env.
    script = tmp_path/'synthetic_crash.py'
    script.write_text('''
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import app.ai_gateway as gateway
from app.models import PayrollRun
engine=create_engine(os.environ['SYNTHETIC_DB_URL'],connect_args={'options':os.environ['SYNTHETIC_DB_OPTIONS']})
gateway.SessionLocal=sessionmaker(engine,autoflush=False)
def crash(plan,actor,*,db,**kwargs):
    db.get(PayrollRun,1).status='approved'
    db.flush()
    os._exit(87)
gateway.execute_plan=crash
gateway.approve_command(os.environ['SYNTHETIC_TOKEN'],'synthetic')
''')
    # Engine connect_args are known from fixture, not introspected secrets.
    with engine.connect() as connection:
        schema=connection.execute(text('SELECT current_schema()')).scalar_one()
    env={**os.environ,'SYNTHETIC_DB_URL':engine.url.render_as_string(hide_password=False),
         'SYNTHETIC_DB_OPTIONS':'-csearch_path='+schema,'SYNTHETIC_TOKEN':token,
         'PYTHONPATH':str(__import__('pathlib').Path(__file__).resolve().parents[1])}
    child=subprocess.run([sys.executable,str(script)],env=env,capture_output=True,timeout=8,check=False)
    assert child.returncode==87,child.stderr.decode()
    with synthetic() as db:
        assert db.get(PayrollRun,1).status=='draft'
        assert db.scalar(select(AICommandApproval)).status=='pending'
        assert db.scalar(select(AuditLog.id).where(AuditLog.action=='ai_command_executed')) is None


def test_real_payroll_executor_uses_reviewed_figures_and_shared_transaction(synthetic):
    from app.models import AttendanceMonth
    with synthetic.begin() as db:
        db.add(AttendanceMonth(period_start=date(2026,1,1),period_end=date(2026,1,31),
            status='approved',created_by='synthetic'))
        item=db.get(PayrollItem,1)
        item.performance_bonus=Decimal(200)
        from app.payroll_workflow import recompute_payroll_item
        recompute_payroll_item(item,db.get(PayrollRun,1))
    token=proposal(synthetic,{'action':'approve_payroll_run','payroll_run_id':1})
    result=gateway.approve_command(token,'synthetic',language='en')
    assert result=='Payroll run #1 was approved.'
    with synthetic() as db:
        assert db.get(PayrollRun,1).status=='approved'
        assert db.get(PayrollItem,1).net_pay==Decimal(1200)
        assert db.scalar(select(AICommandApproval)).status=='consumed'


def test_changed_underlying_source_invalidates_even_unchanged_payroll_items(synthetic,monkeypatch):
    token=proposal(synthetic,{'action':'approve_payroll_run','payroll_run_id':1})
    with synthetic.begin() as db:
        db.get(Employee,1).basic_salary=Decimal(9000)
    monkeypatch.setattr(gateway,'execute_plan',lambda *args,**kwargs: pytest.fail('stale source execution'))
    with pytest.raises(ValueError):
        gateway.approve_command(token,'synthetic')
    with synthetic() as db:
        assert db.get(PayrollRun,1).status=='draft'
        assert db.scalar(select(AICommandApproval)).status=='invalidated'


def test_missing_snapshot_is_never_a_legacy_bypass(synthetic,monkeypatch):
    token=proposal(synthetic,{'action':'approve_payroll_run','payroll_run_id':1})
    with synthetic.begin() as db:
        record=db.scalar(select(AICommandApproval))
        plan={'action':'approve_payroll_run','payroll_run_id':1}
        record.plan_json=gateway._canonical_json(plan)
        record.plan_hash=gateway.plan_hash(plan)
    monkeypatch.setattr(gateway,'execute_plan',lambda *args,**kwargs: pytest.fail('missing snapshot execution'))
    with pytest.raises(ValueError):
        gateway.approve_command(token,'synthetic')
    with synthetic() as db:
        assert db.scalar(select(AICommandApproval)).status=='invalidated'


def test_human_coverage_is_independent_of_model_and_current_snapshot(synthetic,monkeypatch):
    from app.models import AttendanceMonth
    with synthetic.begin() as db:
        db.add(AttendanceMonth(period_start=date(2026,1,1),period_end=date(2026,1,31),
            status='under_review',created_by='synthetic'))
    token=proposal(synthetic,{'action':'approve_attendance_month','month_id':1,'coverage_confirmed':True})
    monkeypatch.setattr(gateway,'execute_plan',lambda *args,**kwargs: pytest.fail('no human attestation'))
    with pytest.raises(ValueError,match='coverage'):
        gateway.approve_command(token,'synthetic',language='en')
    with synthetic() as db:
        assert db.scalar(select(AICommandApproval)).status=='pending'
        assert db.get(AttendanceMonth,1).status=='under_review'


@pytest.mark.parametrize('language,expected', [('ar','بيانات'),('en','Invalid'),('zh','指令')])
def test_validation_messages_match_ui_language(language,expected):
    with pytest.raises(ValueError,match=expected):
        validate_plan_values({'is_financial':'false'},language)


def test_competing_write_timeout_never_consumes_token(synthetic):
    if synthetic.kw['bind'].dialect.name != 'postgresql':
        pytest.skip('PostgreSQL lock timeout only')
    from concurrent.futures import ThreadPoolExecutor

    from sqlalchemy.exc import OperationalError
    token=proposal(synthetic,{'action':'approve_payroll_run','payroll_run_id':1})
    with synthetic.begin() as writer:
        writer.execute(text('UPDATE payroll_items SET performance_bonus=100 WHERE id=1'))
        with ThreadPoolExecutor(max_workers=1) as pool:
            attempt=pool.submit(gateway.approve_command,token,'synthetic')
            with pytest.raises(OperationalError,match='lock timeout'):
                attempt.result(timeout=5)
    with synthetic() as db:
        assert db.get(PayrollRun,1).status=='draft'
        assert db.scalar(select(AICommandApproval)).status=='pending'


def test_outcome_render_failure_rolls_back_business_and_token(synthetic,monkeypatch):
    token=proposal(synthetic,{'action':'approve_payroll_run','payroll_run_id':1})
    def execute(plan,actor,*,db,**kwargs):
        db.get(PayrollRun,1).status='approved'
        db.flush()
        return agent.ExecutionResult('approve_payroll_run',{'run_id':1})
    monkeypatch.setattr(gateway,'execute_plan',execute)
    monkeypatch.setattr(gateway,'render_execution_result',lambda *args:
        (_ for _ in ()).throw(RuntimeError('Synthetic render failure')))
    with pytest.raises(RuntimeError,match='render failure'):
        gateway.approve_command(token,'synthetic')
    with synthetic() as db:
        assert db.get(PayrollRun,1).status=='draft'
        assert db.scalar(select(AICommandApproval)).status=='pending'


def test_review_receipt_is_actor_token_and_plan_bound(synthetic):
    token=proposal(synthetic,{'action':'approve_payroll_run','payroll_run_id':1})
    pending=gateway.review_command(token,'synthetic','en')
    assert pending['reviewed'] is True
    assert gateway.verify_review_receipt(token,'synthetic',pending['review_receipt'])
    assert not gateway.verify_review_receipt(token,'other',pending['review_receipt'])
    assert not gateway.verify_review_receipt(token,'synthetic','wrong')
    with synthetic.begin() as db:
        record=db.scalar(select(AICommandApproval))
        record.plan_hash='0'*64
    assert not gateway.verify_review_receipt(token,'synthetic',pending['review_receipt'])


def test_separate_review_refuses_stale_financial_snapshot(synthetic):
    token=proposal(synthetic,{'action':'approve_payroll_run','payroll_run_id':1})
    with synthetic.begin() as db:
        db.get(PayrollItem,1).performance_bonus=Decimal(800)
    with pytest.raises(ValueError,match='Data changed'):
        gateway.review_command(token,'synthetic','en')
