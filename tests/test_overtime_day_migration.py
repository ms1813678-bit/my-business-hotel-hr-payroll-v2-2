"""Changed unpublished migration preserves pre-existing financial snapshots."""
from uuid import uuid4
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from alembic import command
from alembic.config import Config
from app.db import engine
import pytest


def test_existing_draft_and_frozen_values_preserved():
    if engine.dialect.name!='postgresql':pytest.skip('Migrated PostgreSQL required')
    schema='synthetic_ot_migration_'+uuid4().hex
    with engine.begin() as conn:conn.execute(text('CREATE SCHEMA '+schema))
    local=create_engine(engine.url,connect_args={'options':'-csearch_path='+schema})
    try:
        with local.begin() as conn:
            cfg=Config('alembic.ini');cfg.attributes['connection']=conn;command.upgrade(cfg,'0064_reconciliation_approval')
            conn.execute(text("INSERT INTO departments(id,name,active) VALUES(1,'Synthetic migration',true)"))
            conn.execute(text("INSERT INTO employees(id,code,full_name,department_id,position,hire_date,status,basic_salary,daily_hours) VALUES(1,'SYN-MIGRATION','Synthetic',1,'Synthetic','2025-01-01','active',6000,8)"))
            for i,status in ((1,'draft'),(2,'approved'),(3,'closed'),(4,'reopened')):
                conn.execute(text("INSERT INTO payroll_runs(id,period_start,period_end,pay_date,status,created_by,created_at) VALUES(:id,make_date(2026,:id,1),make_date(2026,:id,28),make_date(2026,:id,28),:status,'synthetic',now())"),dict(id=i,status=status))
                conn.execute(text("INSERT INTO payroll_items(id,payroll_run_id,employee_id,employee_code_snapshot,employee_name_snapshot,department_name_snapshot,position_snapshot,salary_basis,base_salary,daily_hours,overtime_hours,overtime_amount,net_pay,attendance_bonus,performance_bonus,occupancy_bonus,other_bonus,absence_days,absence_deduction,lateness_deduction,late_minutes,early_minutes,insurance_share,taxes,other_deductions,advance_deduction,staff_reward_amount,staff_sanction_amount,note,created_at,updated_at) VALUES(:id,:id,1,'SYN','Synthetic','Synthetic','Synthetic',6000,6000,8,3.25,81.25,6081.25,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,'Original entered provenance',now(),now())"),dict(id=i))
            conn.execute(text("INSERT INTO payroll_payments(payroll_item_id,amount,paid_at,method,created_by,created_at) VALUES(4,1,now(),'cash','synthetic',now())"))
            before=conn.execute(text('SELECT id,overtime_hours,overtime_amount,net_pay,note FROM payroll_items ORDER BY id')).all()
            command.upgrade(cfg,'head')
            after=conn.execute(text('SELECT id,overtime_hours,overtime_amount,net_pay,note FROM payroll_items ORDER BY id')).all()
            assert before==after
            assert conn.execute(text('SELECT id,overtime_daily_required FROM payroll_runs ORDER BY id')).all()==[(1,True),(2,False),(3,False),(4,True)]
            # Exercise the real approval operation, not only the migrated flag.
            from app.models import AttendanceMonth,PayrollRun
            from app.payroll_workflow import approve_payroll_run
            with Session(bind=conn) as db:
                run=db.get(PayrollRun,1)
                db.add(AttendanceMonth(period_start=run.period_start,period_end=run.period_end,status='approved',created_by='synthetic'));db.flush()
                with pytest.raises(ValueError,match='daily review'):
                    approve_payroll_run(db,run,actor='synthetic')
            command.downgrade(cfg,'0064_reconciliation_approval')
            assert conn.execute(text('SELECT id,overtime_hours,overtime_amount,net_pay,note FROM payroll_items ORDER BY id')).all()==before
    finally:
        local.dispose()
        with engine.begin() as conn:conn.execute(text('DROP SCHEMA '+schema+' CASCADE'))
