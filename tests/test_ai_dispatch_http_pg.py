"""New write phrases reach the real confirmation gateway, not execution.

Synthetic migrated PG schema; only model planning is deterministic, no real model.
The existing HTTP fixture uses real login/CSRF/current auth, overriding DB wiring.
"""
from datetime import date
from decimal import Decimal
import pytest
from sqlalchemy import select,func
from app import ai_gateway
from app.models import LeaveRecord,AICommandApproval,AuditLog
from tests.test_ai_error_http_pg import pg_http,login,receipt
from tests.test_routes_security import csrf_from

@pytest.mark.parametrize('lang,phrase',[
 ('ar','لو سمحت اعتمد مسودة الإجازة رقم 1'),
 ('en','Please approve the draft leave record 1'),
 ('zh','请批准休假草稿1'),
])
def test_new_polite_write_phrase_only_proposes_then_requires_explicit_review(pg_http,monkeypatch,lang,phrase):
 client,factory,ids=pg_http;login(client);client.cookies.set('my_hr_language',lang)
 with factory.begin() as db:
  row=LeaveRecord(employee_id=ids[0],leave_type_id=ids[1],start_date=date(2027,1,1),end_date=date(2027,1,1),calendar_days=1,charged_days=Decimal(1),reason='Synthetic',status='draft',created_by='synthetic')
  db.add(row);db.flush();rid=row.id
  before=db.scalar(select(func.count()).select_from(AuditLog))
 calls=[]
 def plan(question,*args,**kwargs):
  calls.append(question)
  return dict(action='confirm_leave',record_id=rid,reason='Synthetic')
 monkeypatch.setattr(ai_gateway,'build_plan',plan)
 page=client.get('/ai')
 assert client.post('/ai',data={'question':phrase}).status_code==403
 response=client.post('/ai',data={'question':phrase,'csrf_token':csrf_from(page)})
 assert response.status_code==200
 assert calls==[phrase]
 with factory() as db:
  pending=db.scalar(select(AICommandApproval));assert pending.status=='pending'
  assert db.get(LeaveRecord,rid).status=='draft'
  audits=list(db.scalars(select(AuditLog)))
  assert len(audits)==before+1
  assert audits[-1].action=='ai_command_proposed'
 import re
 match=re.search(r'name="token" value="([^"]+)"',response.text);assert match,response.text[-3000:]
 token=match.group(1)
 denied=client.post('/ai/approve',data={'token':token,'csrf_token':csrf_from(response)})
 assert denied.status_code in (200,400,422)
 with factory() as db:assert db.get(LeaveRecord,rid).status=='draft'
 review=client.post('/ai/review',data={'token':token,'csrf_token':csrf_from(response)})
 proof=receipt(review)
 approved=client.post('/ai/approve',data={'token':token,'csrf_token':csrf_from(review),'review_receipt':proof})
 assert approved.status_code==200
 with factory() as db:
  assert db.get(LeaveRecord,rid).status=='confirmed'
  assert db.scalar(select(AICommandApproval)).status=='consumed'
