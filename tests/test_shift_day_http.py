"""Standalone real migrated DB HTTP fixture, synthetic rollback/cleanup database."""
import os,re,json
from datetime import date,time,datetime,UTC
from decimal import Decimal
from hashlib import sha256
from dataclasses import replace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select,func,inspect
from app import main
from app.db import SessionLocal
from app.models import AdminAccount,Employee,Department,ShiftPattern,DepartmentWorkbookExport,DepartmentWorkbookPreparation,DepartmentFingerprintPreparation,AttendanceDailyRecord,AuditLog
from app.security import hash_password
from app.department_workbook import build_department_workbook
from app.department_manifest import encode_manifest
from app.restore_lock import read_restore_epoch
from tests.test_department_workbook import make_sample

pytestmark=pytest.mark.skipif(os.getenv('MANUAL_SHIFT_HTTP')!='1',reason='Dedicated disposable DB required')


def seed():
    with SessionLocal.begin() as db:
        department=Department(name='Synthetic shift HTTP',active=True);db.add(department);db.flush()
        employee=Employee(code='SYN-SHIFT-HTTP',full_name='Synthetic shift person',department_id=department.id,position='Synthetic',hire_date=date(2025,1,1),status='active',basic_salary=Decimal(1),daily_hours=Decimal(8))
        db.add(employee)
        for role in ('system_admin','finance'):
            db.add(AdminAccount(username='synthetic-shift-'+role,password_hash=hash_password('Synthetic-Only-Login-1004!'),role=role,active=True,must_change_password=False))
        db.flush();actor=db.scalar(select(AdminAccount).where(AdminAccount.role=='system_admin'))
        _raw,manifest=make_sample(date(2026,11,1),1)
        source_employee=replace(manifest.employees[0],employee_id=employee.id,code=employee.code,name=employee.full_name)
        from pathlib import Path
        _raw,manifest=build_department_workbook(department=department.name,month_start=date(2026,11,1),employees=(source_employee,),approved_print_logo=Path('app/static/hotel-logo-print-approved.png'))
        text=encode_manifest(manifest);epoch=read_restore_epoch(db)
        export=DepartmentWorkbookExport(id='a'*64,actor_id=actor.id,manifest_json=text,manifest_sha256=sha256(text.encode()).hexdigest(),source_digest='b'*64,restore_epoch=epoch,created_at=datetime.now(UTC))
        db.add(export);db.flush()
        days=json.dumps([dict(employee_id=employee.id,date=f'2026-11-{day:02}',code='P' if day==1 else None,raw_value='P' if day==1 else None,sheet='Synthetic',cell=f'C{day}') for day in range(1,31)])
        receipt=DepartmentWorkbookPreparation(export_id=export.id,return_sha256='c'*64,actor_id=actor.id,evidence_json=days,evidence_sha256=sha256(days.encode()).hexdigest(),created_at=datetime.now(UTC))
        db.add(receipt);db.flush()
        raw=b'ID,Date,Time\nSYN-SHIFT-HTTP,2026-11-01,23:00 23:00\nSYN-SHIFT-HTTP,2026-11-02,07:00\n'
        review=json.dumps(dict(version=2,alias_bindings=[[employee.code,employee.code.casefold(),employee.id]],alias_bindings_sha256=sha256(json.dumps([[employee.code,employee.code.casefold(),employee.id]],ensure_ascii=False,separators=(',',':')).encode()).hexdigest(),export_id=export.id,department_sha256=receipt.return_sha256,fingerprint_sha256=sha256(raw).hexdigest(),days=[dict(employee_id=v['employee_id'],date=v['date'],department_code=v['code'],sheet=v['sheet'],cell=v['cell']) for v in json.loads(days)]))
        db.add(DepartmentFingerprintPreparation(id='d'*64,return_id=receipt.id,actor_id=actor.id,fingerprint_bytes=raw,fingerprint_sha256=sha256(raw).hexdigest(),review_json=review,review_sha256=sha256(review.encode()).hexdigest(),restore_epoch=epoch,created_at=datetime.now(UTC)))
        pattern=ShiftPattern(department_id=department.id,code='SYN-NIGHT',name='Synthetic night',start_time=time(23),end_time=time(7),match_before_minutes=0,match_after_minutes=0,active=True)
        db.add(pattern);db.flush()
        return employee.id,pattern.id


def token(response):return re.search(r'name="csrf_token" value="([^"]+)"',response.text).group(1)


def test_standalone_actual_daylist_detail_no_effect(monkeypatch):
    monkeypatch.setattr(main,'send_login_alert',lambda *a,**k:None)
    monkeypatch.setattr(main,'send_telegram_login_event',lambda *a,**k:None)
    employee_id,pattern_id=seed()
    with TestClient(main.app) as client:
        url=f'/attendance/shift-evidence/{"d"*64}/{employee_id}'
        assert client.get(url).status_code==401
        assert client.post('/login',data=dict(csrf_token=token(client.get('/login')),username='synthetic-shift-system_admin',password='Synthetic-Only-Login-1004!'),follow_redirects=False).status_code==303
        listing=client.get(url);assert listing.status_code==200 and 'Synthetic shift person' in listing.text
        assert 'shift-day-detail' not in listing.text
        detail=client.get(url,params={'work_date':'2026-11-01'});assert detail.status_code==200 and 'shift-day-detail' in detail.text
        fields=dict(csrf_token=token(detail),work_date='2026-11-01',windows=f'2026-11-01:{pattern_id}')
        assert client.post(url,data={k:v for k,v in fields.items() if k!='csrf_token'}).status_code==403
        for updates in (dict(work_date='2026-12-01'),dict(windows=f'2026-11-01:{pattern_id+9999}'),dict(extra='1')):
            assert client.post(url,data={**fields,**updates}).status_code==400
        assert client.get(url+'/bad').status_code==404
        assert client.get(url,params={'work_date':'2026-12-01'}).status_code==404
        def state():
            with SessionLocal() as db:
                return tuple(tuple(tuple(getattr(row,c.key) for c in inspect(m).columns) for row in db.scalars(select(m).order_by(m.id))) for m in (DepartmentWorkbookExport,DepartmentWorkbookPreparation,DepartmentFingerprintPreparation,AttendanceDailyRecord,AuditLog))
        before=state();result=client.post(url,data=fields)
        assert result.status_code==200 and result.headers['cache-control']=='no-store'
        assert result.text.count('اتجاه البصمة غير معروف')==3
        assert state()==before
        for lang in ('en','zh'):
            client.cookies.set('my_hr_language',lang)
            localized=client.post(url,data=fields)
            content=localized.text.split('<main class="content">',1)[1].split('<footer>',1)[0]
            assert re.search(r'[\u0600-\u06ff]',content) is None
        client.cookies.clear()
        assert client.post('/login',data=dict(csrf_token=token(client.get('/login')),username='synthetic-shift-finance',password='Synthetic-Only-Login-1004!'),follow_redirects=False).status_code==303
        assert client.get(url).status_code==403
        assert client.post(url,data={**fields,'csrf_token':token(client.get('/payroll'))}).status_code==403
