"""Actual route handler + AI execute_plan, dedicated disposable actual-Base PG schema."""
from sqlalchemy import create_engine,text,select,func,event,delete
from sqlalchemy.orm import Session,sessionmaker
from app.db import Base
from app.models import Department,Employee,LeaveType,LeaveRecord,AuditLog,LeaveBalanceTransaction,AdminAccount
from app.ai_agent import execute_plan
import app.main as main
from datetime import date
from decimal import Decimal
import threading,json,time,os


def test_real_pg_cooperative_create_and_gateway_table_order():
    from app.db import engine as configured_engine
    import uuid,pytest
    if configured_engine.dialect.name != 'postgresql':pytest.skip('PostgreSQL row locks required', allow_module_level=True)
    # Never create even disposable schemas in a production-named database.
    name=configured_engine.url.database or ''
    if not (name.startswith(('test_', 'synthetic_', 'checklist')) or name.endswith('_test')):
        pytest.skip('explicit disposable test database name required')
    if os.environ.get('PYTEST_XDIST_WORKER'):
        pytest.skip('handler SessionLocal patch requires non-xdist execution')
    url=configured_engine.url;admin=create_engine(url);schema='leave_lock_'+uuid.uuid4().hex[:12]
    with admin.begin() as c:c.execute(text('CREATE SCHEMA '+schema))
    e=create_engine(url,connect_args={'options':'-csearch_path='+schema,'application_name':schema});factory=sessionmaker(e);old=main.SessionLocal;main.SessionLocal=factory;out=[]
    try:
     with admin.connect() as check:
      assert check.execute(text("SHOW transaction_isolation")).scalar()=="read committed"
     Base.metadata.create_all(e)
     with factory() as db:
      d=Department(name='Synthetic');db.add(d);db.flush();emp=Employee(code='SYN',full_name='Synthetic',department_id=d.id,position='T',hire_date=date(2020,1,1),status='active',basic_salary=100,daily_hours=8);t=LeaveType(code='annual',name_ar='سنوية',name_en='Annual');db.add_all([emp,t]);db.commit();eid,tid=emp.id,t.id
     args=dict(employee_id=eid,leave_type_id=tid,start_date=date(2027,12,10),end_date=date(2027,12,11),charged_days=Decimal('2'),reason='Synthetic')
     def invoke(kind):
      if kind=='route':
       response=main.create_leave_record(**args,admin=AdminAccount(username='test'));return response.headers['location']
      plan=dict(action='create_leave',**args);plan['start_date']=plan['start_date'].isoformat();plan['end_date']=plan['end_date'].isoformat()
      try:
       with factory.begin() as db:
        if kind=='gateway':
         from app.ai_gateway import _lock_command_business_tables
         _lock_command_business_tables(db)
        execute_plan(plan,'test',db=db)
       return 'applied'
      except ValueError:return 'overlap_rejected'
     for first,second,rollback in [('route','ai',False),('ai','route',False),('route','route',False),('ai','ai',False),('route','ai',True),('route','gateway',False),('gateway','route',False)]:
      with e.begin() as c:c.execute(delete(AuditLog));c.execute(delete(LeaveRecord))
      checked=threading.Event();release=threading.Event();finished=threading.Event();results={};first_thread=[None]
      @event.listens_for(e,'after_cursor_execute')
      def hook(conn,cur,stmt,params,ctx,many):
       if threading.current_thread() is first_thread[0]:
        if stmt.startswith('SELECT leave_records.id') and 'LIMIT' in stmt:checked.set();assert release.wait(5)
        if rollback and stmt.startswith('INSERT INTO leave_records'):raise RuntimeError('injected rollback')
      def run(label,kind):
       try:results[label]=invoke(kind)
       except Exception as ex:results[label]=type(ex).__name__
       if label=='second':finished.set()
      a=threading.Thread(target=run,args=('first',first));first_thread[0]=a;a.start();assert checked.wait(5)
      b=threading.Thread(target=run,args=('second',second));b.start();blocked=not finished.wait(.2)
      waiting=0
      deadline=time.monotonic()+2
      while time.monotonic()<deadline and not finished.is_set():
       with admin.connect() as observer:
        waiting=observer.scalar(text("SELECT count(*) FROM pg_stat_activity WHERE application_name=:app AND wait_event_type='Lock'"),{'app':schema})
       if waiting>=1:break
       time.sleep(.02)
      assert waiting>=1
      release.set();a.join(6);b.join(6);assert not a.is_alive() and not b.is_alive()
      event.remove(e,'after_cursor_execute',hook)
      with factory() as db:count=db.scalar(select(func.count()).select_from(LeaveRecord));audits=db.scalar(select(func.count()).select_from(AuditLog));ledger=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
      assert blocked and count==1 and audits==1 and ledger==0
      if not rollback:assert results['second'] in ('/leave?error=overlap','overlap_rejected')
      else:assert results['first']=='RuntimeError' and results['second']=='applied'
      out.append(dict(first=first,second=second,first_rollback=rollback,second_blocked_before_overlap=blocked,results=results,record_count=count,audit_count=audits,ledger_count=ledger))
     print(json.dumps(out,indent=2))
    finally:
     main.SessionLocal=old;e.dispose()
     with admin.begin() as c:c.execute(text('DROP SCHEMA '+schema+' CASCADE'))
     admin.dispose()
