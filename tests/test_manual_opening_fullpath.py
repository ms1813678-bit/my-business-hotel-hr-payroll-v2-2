"""Committed fixture acceptance. Run ONLY in a fresh disposable full-migration DB.

MANUAL_FULLPATH_ACCEPTANCE=1 DATABASE_URL=<dedicated DB> pytest this_file
Never enables against the normal shared test DB, leaves audit evidence intact.
"""
import os
import re
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import main
from app.db import SessionLocal
from app.models import (
    AdminAccount,
    AuditLog,
    Department,
    Employee,
    LeaveBalanceTransaction,
    LeaveHistoricalEvidencePreparation,
)
from app.security import hash_password

pytestmark = pytest.mark.skipif(os.getenv('MANUAL_FULLPATH_ACCEPTANCE') != '1',
    reason='Requires explicit isolated disposable database acceptance run')


def token(response):
    assert response.status_code == 200
    return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)


def test_full_middleware_login_csrf_three_proposals_retry_role_and_ledger(monkeypatch):
    assert not main.app.dependency_overrides
    # Prevent notification side effects, not authentication/DB/middleware.
    monkeypatch.setattr(main, 'send_login_alert', lambda *a, **k: None)
    monkeypatch.setattr(main, 'send_telegram_login_event', lambda *a, **k: None)
    with SessionLocal.begin() as db:
        assert db.scalar(select(func.count()).select_from(AdminAccount)) == 0
        department = Department(name='Synthetic committed HR', active=True)
        db.add(department); db.flush()
        employee = Employee(code='SYN-FULLPATH', full_name='Synthetic only', department_id=department.id,
            position='Synthetic', hire_date=date(2025,1,1), status='active', basic_salary=Decimal(1), daily_hours=Decimal(8))
        db.add(employee)
        for role in ('system_admin', 'finance', 'assistant_manager'):
            db.add(AdminAccount(username='synthetic-'+role, password_hash=hash_password('Synthetic-Only-Login-1004!'),
                role=role, active=True, must_change_password=False))
        db.flush(); employee_id = employee.id
    with SessionLocal() as db:
        ledger_before = db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    with TestClient(main.app) as client:
        assert client.get('/leave/opening').status_code == 401
        assert client.get('/leave/policies').status_code==401
        assert client.post('/leave/policies/confirm',data={}).status_code in (401,403)
        for role in ('system_admin', 'finance', 'assistant_manager'):
            client.cookies.clear()
            login = client.post('/login', data={'csrf_token':token(client.get('/login')),
                'username':'synthetic-'+role, 'password':'Synthetic-Only-Login-1004!'}, follow_redirects=False)
            assert login.status_code == 303
            if role != 'system_admin':
                denied_csrf=token(client.get('/login'))
                assert client.get('/leave/opening').status_code == 403
                assert client.get('/leave/departments').status_code==403
                assert client.get('/leave/policies').status_code==403
                for denied_path in ('/leave/policies/preview','/leave/policies/confirm'):
                    assert client.post(denied_path,data={'csrf_token':denied_csrf}).status_code==403
                denied_files={'department_workbook':('synthetic.xlsx',b'invalid'),'fingerprint_workbook':('synthetic.xlsx',b'invalid')}
                assert client.post('/leave/departments/compare',data={'csrf_token':denied_csrf,'export_id':'a'*64},files=denied_files).status_code==403

                for path, fields in [('/leave/opening/policy-defaults',{'group_id':'a'*64}),('/leave/opening/confirm-source',{'group_id':'a'*64,'policy_id':'b'*64,'source_sha256':'c'*64,'reason':'Synthetic'})]:
                    assert client.post(path,data={**fields,'csrf_token':denied_csrf}).status_code==403
                continue
            csrf = token(client.get('/leave/opening'))
            data = {'employee_id':employee_id, 'effective_start':'2026-05-17',
                'annual':'8.250000000000000000000001', 'weekly':'2', 'holiday':'0', 'reason':'Synthetic full middleware'}
            assert client.post('/leave/opening/review', data=data).status_code == 403
            data['csrf_token'] = csrf
            response = client.post('/leave/opening/review', data=data)
            assert response.status_code == 200 and '8.250000000000000000000001' in response.text
            assert '2026-05-17' in response.text
            assert '/leave/opening/groups/' in str(response.url)
            assert client.get(str(response.url)).status_code == 200
            assert str(response.url).split('/leave/opening/groups/')[1] in client.get('/leave/opening').text
            assert client.post('/leave/opening/review', data=data).status_code == 200
            saved_url=str(response.url)
            group_id=saved_url.rsplit('/',1)[1]
            policy_response=client.post('/leave/opening/policy-defaults',data={
                'csrf_token':csrf,'group_id':group_id})
            assert policy_response.status_code==200
            policy_id=str(policy_response.url).split('policy_id=')[1]
            assert 'action="/leave/opening/confirm-source"' in policy_response.text
            digest=re.search(r'name="source_sha256" value="([^"]+)"',policy_response.text).group(1)
            confirmation={'csrf_token':csrf,'group_id':group_id,'policy_id':policy_id,
                'source_sha256':digest,'previous_id':'','reason':'Synthetic fullmain confirmation','human_confirmed':'confirmed'}
            assert client.post('/leave/opening/confirm-source',data={**confirmation,'source_sha256':'0'*64},
                follow_redirects=False).headers['location']=='/leave/opening?error=1'
            confirmed=client.post('/leave/opening/confirm-source',data=confirmation)
            assert confirmed.status_code==200 and 'role="status"' in confirmed.text
            assert client.post('/leave/opening/confirm-source',data=confirmation).status_code==200
            policy_page=client.get('/leave/policies',params={'base_id':policy_id})
            assert policy_page.status_code==200 and 'action="/leave/policies/preview"' in policy_page.text
            from app.models import LeaveOpeningSelection
            from sqlalchemy import inspect as sa_inspect
            def selection_snapshot():
                with SessionLocal() as source_db:
                    return [tuple(getattr(row,c.key) for c in sa_inspect(LeaveOpeningSelection).columns) for row in source_db.scalars(select(LeaveOpeningSelection).order_by(LeaveOpeningSelection.id))]
            source_before_policy=selection_snapshot()
            from tests.test_leave_policy_edit import fields as policy_fields
            editing={**policy_fields(),'base_id':policy_id,'reason':'Synthetic settings workflow','annual_deduct_sick':'true'}
            assert client.post('/leave/policies/preview',data=editing).status_code==403
            preview=client.post('/leave/policies/preview',data={**editing,'csrf_token':csrf})
            assert preview.status_code==200 and 'action="/leave/policies/confirm"' in preview.text
            draft_id=re.search(r'name="draft_id" value="([^"\s]+)"',preview.text).group(1)
            draft_hash=re.search(r'name="reviewed_sha256" value="([^"\s]+)"',preview.text).group(1)
            confirm_fields={'csrf_token':csrf,'draft_id':draft_id,'reviewed_sha256':draft_hash,'human_confirmed':'confirmed'}
            from app.models import LeavePolicyEditDraft,LeavePolicyVersion
            with SessionLocal() as verify_db:
                refusal_before=[verify_db.scalar(select(func.count()).select_from(m)) for m in (LeavePolicyEditDraft,LeavePolicyVersion,AuditLog)]
            assert client.post('/leave/policies/confirm',data={k:v for k,v in confirm_fields.items() if k!='csrf_token'}).status_code==403
            assert client.post('/leave/policies/confirm',data={**confirm_fields,'extra':'1'},follow_redirects=False).headers['location']=='/leave/policies?error=1'
            assert client.post('/leave/policies/preview',data={**editing,'csrf_token':csrf,'extra':'1'},follow_redirects=False).headers['location']=='/leave/policies?error=1'
            duplicate='&'.join(k+'='+v for k,v in confirm_fields.items())+'&draft_id='+draft_id
            assert client.post('/leave/policies/confirm',content=duplicate,headers={'content-type':'application/x-www-form-urlencoded'},follow_redirects=False).headers['location']=='/leave/policies?error=1'
            with SessionLocal() as verify_db:
                assert [verify_db.scalar(select(func.count()).select_from(m)) for m in (LeavePolicyEditDraft,LeavePolicyVersion,AuditLog)]==refusal_before
            with TestClient(main.app) as other_client:
                with SessionLocal.begin() as other_db:
                    other_db.add(AdminAccount(username='synthetic-policy-other',password_hash=hash_password('Synthetic-Only-Login-1004!'),role='system_admin',active=True))
                assert other_client.post('/login',data={'csrf_token':token(other_client.get('/login')),'username':'synthetic-policy-other','password':'Synthetic-Only-Login-1004!'},follow_redirects=False).status_code==303
                other_csrf=token(other_client.get('/login'))
                assert other_client.get('/leave/policies',params={'draft_id':draft_id}).status_code==404
                assert other_client.post('/leave/policies/confirm',data={**confirm_fields,'csrf_token':other_csrf},follow_redirects=False).headers['location']=='/leave/policies?error=1'
            confirmed_policy=client.post('/leave/policies/confirm' ,data={'csrf_token':csrf,'draft_id':draft_id,'reviewed_sha256':draft_hash,'human_confirmed':'confirmed'})
            assert confirmed_policy.status_code==200 and 'action="/leave/policies/confirm"' not in confirmed_policy.text
            assert client.post('/leave/policies/confirm',data={'csrf_token':csrf,'draft_id':draft_id,'reviewed_sha256':'0'*64,'human_confirmed':'confirmed'},follow_redirects=False).headers['location']=='/leave/policies?error=1'

            assert selection_snapshot()==source_before_policy
            identical=client.post('/leave/policies/preview',data={**policy_fields(),'base_id':policy_id,'reason':'Synthetic identical HTTP','csrf_token':csrf})
            assert identical.status_code==200 and policy_id in identical.text
            assert 'الإعدادات مطابقة لنسخة محفوظة.' in identical.text
            assert 'value="'+policy_id+'" selected' in identical.text
            from app.models import LeaveOpeningSelection
            from app.models import EmployeeServicePeriod
            with SessionLocal.begin() as setup_db:
                selected_employee=setup_db.get(Employee,employee_id)
                selected_employee.birth_date=date(2000,1,1)
                selected_employee.prior_insurance_years=Decimal(0)
                setup_db.add(EmployeeServicePeriod(employee_id=employee_id,start_date=date(2025,1,1)))
                department_id=selected_employee.department_id
                selection_id=setup_db.scalar(select(LeaveOpeningSelection.id))
            workspace=client.get('/leave/departments',params={'department_id':department_id,'month':'2026-05'})
            assert workspace.status_code==200 and selection_id in workspace.text
            export_fields={'csrf_token':csrf,'department_id':department_id,'month':'2026-05','selection_ids':selection_id}
            for month_index in range(1,6):
                month_date=date(2026,month_index,1).isoformat()
                export_fields['age_'+month_date]=month_date
                export_fields['insurance_'+str(employee_id)+'_'+month_date]='0'
            exported=client.post('/leave/departments/export',data=export_fields)
            assert exported.status_code==200
            assert exported.headers['content-type'].startswith('application/vnd.openxmlformats')
            export_id=re.search('department-([0-9a-f]{64})',exported.headers['content-disposition']).group(1)
            returned=client.post('/leave/departments/return',data={'csrf_token':csrf,'export_id':export_id},
                files={'workbook':('synthetic.xlsx',exported.content,'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')})
            assert returned.status_code==200 and 'role="status"' in returned.text
            assert '2026-05-17' in returned.text
            receipt_id=re.search(r'receipt=([0-9]+)',str(returned.url)).group(1)
            from app.models import DepartmentWorkbookPreparation
            import json
            with SessionLocal.begin() as receipt_db:
                saved=receipt_db.get(DepartmentWorkbookPreparation,int(receipt_id))
                original_receipt=saved.evidence_json
                original_receipt_hash=saved.evidence_sha256
                saved.evidence_json=json.dumps(json.loads(original_receipt)*7)
                from hashlib import sha256
                saved.evidence_sha256=sha256(saved.evidence_json.encode()).hexdigest()
            later_receipt=client.get('/leave/departments',params={'receipt':receipt_id,'receipt_page':1})
            assert later_receipt.status_code==200 and '201 - 217 / 217' in later_receipt.text
            assert 'receipt_page=0' in later_receipt.text
            assert ('name="export_id" value="'+export_id+'"') in later_receipt.text
            assert client.get('/leave/departments',params={'receipt':receipt_id,'receipt_page':2}).status_code==404
            assert client.get('/leave/departments?receipt=2147483647').status_code==404
            assert ('/leave/departments?receipt='+receipt_id+'"') in client.get('/leave/departments').text
            receipt_clones=[]
            with SessionLocal.begin() as history_db:
                saved=history_db.get(DepartmentWorkbookPreparation,int(receipt_id))
                for i in range(21):
                    clone=DepartmentWorkbookPreparation(export_id=export_id,return_sha256=format(i+1,'064x'),actor_id=saved.actor_id,
                        evidence_json=original_receipt,evidence_sha256=original_receipt_hash,created_at=saved.created_at)
                    history_db.add(clone);history_db.flush();receipt_clones.append(clone.id)
            receipt_history=client.get('/leave/departments')
            older_link=re.search(r'href="(/leave/departments\?receipt_before=[0-9]+)"',receipt_history.text).group(1)
            assert ('/leave/departments?receipt='+receipt_id+'"') in client.get(older_link).text
            for invalid in ('²','9'*100,'2147483648','2147483647'):
                assert client.get('/leave/departments',params={'receipt_before':invalid}).status_code==404
            with SessionLocal.begin() as history_db:
                from sqlalchemy import delete
                history_db.execute(delete(DepartmentWorkbookPreparation).where(DepartmentWorkbookPreparation.id.in_(receipt_clones)))

            with SessionLocal.begin() as receipt_db:
                receipt_db.get(DepartmentWorkbookPreparation,int(receipt_id)).evidence_json=original_receipt
                receipt_db.get(DepartmentWorkbookPreparation,int(receipt_id)).evidence_sha256=original_receipt_hash
            with SessionLocal.begin() as receipt_db:
                receipt_db.get(DepartmentWorkbookPreparation,int(receipt_id)).evidence_json+=' '
            assert client.get('/leave/departments?receipt='+receipt_id).status_code==404
            with SessionLocal.begin() as receipt_db:
                saved=receipt_db.get(DepartmentWorkbookPreparation,int(receipt_id))
                malformed=json.loads(original_receipt);malformed[0]['code']=[]
                saved.evidence_json=json.dumps(malformed)
                saved.evidence_sha256=sha256(saved.evidence_json.encode()).hexdigest()
            assert client.get('/leave/departments?receipt='+receipt_id).status_code==404
            with SessionLocal.begin() as receipt_db:
                receipt_db.get(DepartmentWorkbookPreparation,int(receipt_id)).evidence_sha256=original_receipt_hash
            with SessionLocal.begin() as receipt_db:
                receipt_db.get(DepartmentWorkbookPreparation,int(receipt_id)).evidence_json=original_receipt

            from tests.test_attendance_import import workbook_bytes
            from app.models import DepartmentFingerprintPreparation,LeaveRegisterEntry
            fingerprint=workbook_bytes([('Synthetic fingerprint',[
                ['ID','Date','Time'],['SYN-FULLPATH',date(2026,5,17),'08:00 08:00 bad 16:00']])])
            files={'department_workbook':('department.xlsx',exported.content), 'fingerprint_workbook':('fingerprint.xlsx',fingerprint)}
            assert client.post('/leave/departments/compare',data={'export_id':export_id},files=files).status_code==403
            rejected=client.post('/leave/departments/compare',data={'csrf_token':csrf,'export_id':export_id},
                files={**files,'fingerprint_workbook':('invalid.xls',b'not a workbook')},follow_redirects=False)
            assert rejected.status_code==303 and rejected.headers['location']=='/leave/departments?error=1'
            from io import BytesIO
            from zipfile import ZipFile
            missing_parts=BytesIO()
            with ZipFile(missing_parts,'w') as z: z.writestr('synthetic.txt','Synthetic')
            for filename,payload in [('corrupt.xls',b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'+b'\0'*600),('missing.xlsx',missing_parts.getvalue())]:
                rejected=client.post('/leave/departments/compare',data={'csrf_token':csrf,'export_id':export_id},
                    files={**files,'fingerprint_workbook':(filename,payload)},follow_redirects=False)
                assert rejected.status_code==303 and rejected.headers['location']=='/leave/departments?error=1'

            from tests.test_fingerprint_boundaries import malformed_decoder_cases
            for filename,payload in malformed_decoder_cases():
                rejected=client.post('/leave/departments/compare',data={'csrf_token':csrf,'export_id':export_id},
                    files={**files,'fingerprint_workbook':(filename,payload)},follow_redirects=False)
                assert rejected.status_code==303 and rejected.headers['location']=='/leave/departments?error=1'
            comparison=client.post('/leave/departments/compare',data={'csrf_token':csrf,'export_id':export_id},
                files={'department_workbook':('department.xlsx',exported.content),
                    'fingerprint_workbook':('fingerprint.xlsx',fingerprint)})
            assert comparison.status_code==200 and '2026-05-17T08:00:00' in comparison.text
            with SessionLocal() as compare_db:
                stored=compare_db.scalar(select(DepartmentFingerprintPreparation))
                assert stored.fingerprint_bytes==fingerprint
                comparison_id=stored.id
                assert compare_db.scalar(select(func.count()).select_from(DepartmentFingerprintPreparation))==1

                assert 'invalid_time_value' in stored.review_json and 'duplicate_punch' in stored.review_json
                assert compare_db.scalar(select(func.count()).select_from(LeaveRegisterEntry))==0
                assert compare_db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==ledger_before

            with SessionLocal.begin() as tamper_db:
                tamper=tamper_db.get(DepartmentFingerprintPreparation,comparison_id)
                saved_json=tamper.review_json
                tamper.review_json+=' '
            assert client.get('/leave/departments?comparison='+comparison_id).status_code==404
            with SessionLocal.begin() as tamper_db:
                tamper_db.get(DepartmentFingerprintPreparation,comparison_id).review_json=saved_json
            assert client.get('/leave/departments?comparison='+comparison_id).status_code==200
            for invalid_receipt in ('²','9'*100,'2147483648','0','-1'):
                assert client.get('/leave/departments',params={'receipt':invalid_receipt}).status_code==404
            from app.models import DepartmentWorkbookExport
            with SessionLocal.begin() as epoch_db:
                saved_export=epoch_db.get(DepartmentWorkbookExport,export_id)
                original_epoch=saved_export.restore_epoch
                saved_export.restore_epoch='Synthetic mismatched export'
            assert client.get('/leave/departments?receipt='+receipt_id).status_code==404
            assert client.get('/leave/departments?receipt_before='+receipt_id).status_code==404
            assert ('/leave/departments?receipt='+receipt_id+'"') not in client.get('/leave/departments').text

            assert client.get('/leave/departments?comparison='+comparison_id).status_code==404
            assert client.get('/leave/departments?history_before='+comparison_id).status_code==404
            assert ('comparison='+comparison_id) not in client.get('/leave/departments').text
            with SessionLocal.begin() as epoch_db:
                epoch_db.get(DepartmentWorkbookExport,export_id).restore_epoch=original_epoch
            history_page=client.get('/leave/departments')
            assert ('/leave/departments?comparison='+comparison_id) in history_page.text
            assert client.get('/leave/departments?history_before='+comparison_id).status_code==200
            assert client.get('/leave/departments?history_before='+'0'*64).status_code==404
            # Synthetic history clones exercise bounded keyset navigation and epoch filtering.
            clone_ids=[]
            with SessionLocal.begin() as history_db:
                row=history_db.get(DepartmentFingerprintPreparation,comparison_id)
                for i in range(22):
                    clone_id=format(i+1,'064x');clone_ids.append(clone_id)
                    history_db.add(DepartmentFingerprintPreparation(id=clone_id,return_id=row.return_id,actor_id=row.actor_id,
                        fingerprint_bytes=row.fingerprint_bytes,fingerprint_sha256=row.fingerprint_sha256,
                        review_json=row.review_json,review_sha256=row.review_sha256,created_at=row.created_at,
                        restore_epoch='Synthetic other epoch' if i==21 else row.restore_epoch))
            history_page=client.get('/leave/departments')
            assert ('comparison='+clone_ids[-1]) not in history_page.text
            next_cursor=re.search(r'href="(/leave/departments\?history_before=[0-9a-f]{64})"',history_page.text).group(1)
            older=client.get(next_cursor)
            assert older.status_code==200 and ('comparison='+clone_ids[0]) in older.text
            with SessionLocal.begin() as history_db:
                from sqlalchemy import delete
                history_db.execute(delete(DepartmentFingerprintPreparation).where(DepartmentFingerprintPreparation.id.in_(clone_ids)))

            reopened=client.get('/leave/departments?comparison='+comparison_id)
            assert ('name="export_id" value="'+export_id+'"') in reopened.text

            # Bounded synthetic stored-review pagination fixture, not a parser result.
            import json
            from hashlib import sha256
            with SessionLocal.begin() as page_db:
                row=page_db.get(DepartmentFingerprintPreparation,comparison_id)
                original_text=row.review_json
                fixture=json.loads(original_text)
                fixture['days']=fixture['days']*7
                fixture['source_issues']=[{'sheet':'Synthetic pagination','row':i,'reason':'synthetic_issue','value':'Synthetic last issue' if i==60 else 'Synthetic'} for i in range(61)]
                fixture['unmatched_occurrences']=[{'raw_employee_code':'Synthetic','punch_at':'Synthetic last unmatched' if i==60 else 'Synthetic','source_sheet':'Synthetic pagination','source_row':i} for i in range(61)]
                row.review_json=json.dumps(fixture)
                row.review_sha256=sha256(row.review_json.encode()).hexdigest()
            page=client.get('/leave/departments',params={'comparison':comparison_id,'day_page':1,'issue_page':1,'unmatched_page':1})
            assert page.status_code==200 and '201 - 217 / 217' in page.text
            assert 'Synthetic last issue' in page.text and 'Synthetic last unmatched' in page.text
            assert 'day_page=0' in page.text and 'issue_page=0' in page.text
            for key in ('day_page','issue_page','unmatched_page'):
                assert client.get('/leave/departments',params={'comparison':comparison_id,key:99}).status_code==404

            with SessionLocal.begin() as page_db:
                row=page_db.get(DepartmentFingerprintPreparation,comparison_id)
                row.review_json=original_text
                row.review_sha256=sha256(original_text.encode()).hexdigest()


            assert client.post('/leave/departments/export',data={**export_fields,'selection_ids':'0'*64},follow_redirects=False).headers['location']=='/leave/departments?error=1'
            with SessionLocal() as selected_db:
                assert selected_db.scalar(select(func.count()).select_from(LeaveOpeningSelection))==1
                assert selected_db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==ledger_before

            with SessionLocal() as db:
                assert db.scalar(select(func.count()).select_from(LeaveHistoricalEvidencePreparation)) == 3
                assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == ledger_before
                assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='leave_historical_evidence_prepared')) == 3
            for field, value in [('annual','-1'), ('weekly',''), ('holiday','NaN')]:
                bad = dict(data); bad[field] = value
                bad_response = client.post('/leave/opening/review', data=bad, follow_redirects=False)
                assert bad_response.status_code in (303,422)
                if bad_response.status_code == 303:
                    assert bad_response.headers['location'] == '/leave/opening?error=1'
                    assert client.get(bad_response.headers['location']).status_code == 200
            assert client.post('/leave/opening/approve', data=data).status_code in (404,405)
            impact_page=client.get('/leave/policies/impact',params={'draft_id':draft_id,'department_id':department_id,'month':'2026-05'})
            assert impact_page.status_code==200 and selection_id in impact_page.text
            assert 'الأشهر السابقة للتعيين' in impact_page.text
            impact_fields={**export_fields,'draft_id':draft_id,'reviewed_sha256':draft_hash}
            assert client.post('/leave/policies/impact',data={k:v for k,v in impact_fields.items() if k!='csrf_token'}).status_code==403
            assert client.post('/leave/policies/impact',data={**impact_fields,'extra':'1'},follow_redirects=False).headers['location']=='/leave/policies?error=1'
            assert client.post('/leave/policies/impact',data=impact_fields,follow_redirects=False).headers['location']=='/leave/policies?error=1'
            from app.models import LeaveRegisterEntry
            with SessionLocal.begin() as impact_db:
                for day in range(17,32):
                    impact_db.add(LeaveRegisterEntry(employee_id=employee_id,work_date=date(2026,5,day),code='S' if day==18 else '',source='synthetic impact',recorded_by='synthetic'))
            with SessionLocal() as impact_db:
                impact_before=[impact_db.scalar(select(func.count()).select_from(m)) for m in (AuditLog,LeaveBalanceTransaction,LeaveRegisterEntry,LeaveOpeningSelection)]
            impact_result=client.post('/leave/policies/impact',data=impact_fields)
            assert impact_result.status_code==200 and 'نتيجة افتراضية، ليست إعادة حساب معتمدة' in impact_result.text
            assert impact_result.headers['cache-control']=='no-store'
            assert 'ليست أرصدة متاحة أو مُفرجًا عنها.' in impact_result.text
            for invalid_department in ('0','-1','+1','٠١','01','9'*100):
                assert client.post('/leave/policies/impact',data={**impact_fields,'department_id':invalid_department},follow_redirects=False).headers['location']=='/leave/policies?error=1'
            for invalid_month in ('2026-W05','202605','2026-5'):
                assert client.post('/leave/policies/impact',data={**impact_fields,'month':invalid_month},follow_redirects=False).headers['location']=='/leave/policies?error=1'
            assert client.post('/leave/policies/impact',data={**impact_fields,'age_2026-05-01':'2026-W18-1'},follow_redirects=False).headers['location']=='/leave/policies?error=1'
            with SessionLocal.begin() as other_department_db:
                other_department=Department(name='Synthetic other department',active=True)
                other_department_db.add(other_department);other_department_db.flush();other_department_id=other_department.id
            assert client.post('/leave/policies/impact',data={**impact_fields,'department_id':str(other_department_id)},follow_redirects=False).headers['location']=='/leave/policies?error=1'
            with SessionLocal() as wrong_db:
                wrong_base=wrong_db.get(LeavePolicyEditDraft,draft_id).saved_policy_id
            wrong_preview=client.post('/leave/policies/preview',data={**editing,'base_id':wrong_base,'csrf_token':csrf})
            wrong_id=re.search(r'name="draft_id" value="([^"\s]+)"',wrong_preview.text).group(1)
            wrong_hash=re.search(r'name="reviewed_sha256" value="([^"\s]+)"',wrong_preview.text).group(1)
            assert client.post('/leave/policies/impact',data={**impact_fields,'draft_id':wrong_id,'reviewed_sha256':wrong_hash},follow_redirects=False).headers['location']=='/leave/policies?error=1'
            error_impact=client.get('/leave/policies/impact',params={'draft_id':'0'*64})
            assert error_impact.status_code==404 and error_impact.headers['cache-control']=='no-store'
            assert 'إجازة سنوية' in impact_result.text and 'راحة أسبوعية' in impact_result.text
            assert 'SYN-FULLPATH' in impact_result.text and 'Synthetic only' in impact_result.text
            assert 'value="2026-05-01"' in impact_result.text
            assert 'يُحسب الفرق قبل التقريب' in impact_result.text
            assert '2026-05-31' in impact_result.text
            assert selection_snapshot()==source_before_policy
            balance_page=client.get('/leave/balance-explanation',params={'selection_id':selection_id,'month':'2026-05'})
            assert balance_page.status_code==200 and 'SYN-FULLPATH' in balance_page.text
            balance_fields={'csrf_token':csrf,'selection_id':selection_id,'month':'2026-05','release_evaluated_on':'2026-06-01'}
            for month_index in range(1,6):
                month_key=date(2026,month_index,1).isoformat()
                balance_fields['age_'+month_key]=month_key
                balance_fields['insurance_'+month_key]='0'
            assert client.post('/leave/balance-explanation',data={k:v for k,v in balance_fields.items() if k!='csrf_token'}).status_code==403
            assert client.post('/leave/balance-explanation',data={**balance_fields,'extra':'1'},follow_redirects=False).status_code==303
            balance_result=client.post('/leave/balance-explanation',data=balance_fields)
            assert balance_result.status_code==200 and 'الرصيد الحالي والمتاح غير مثبتين' in balance_result.text
            assert 'دفتر مستقل غير مضاف' in balance_result.text and balance_result.headers['cache-control']=='no-store'
            assert selection_snapshot()==source_before_policy
            with SessionLocal() as impact_db:
                assert [impact_db.scalar(select(func.count()).select_from(m)) for m in (AuditLog,LeaveBalanceTransaction,LeaveRegisterEntry,LeaveOpeningSelection)]==impact_before

    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(LeaveHistoricalEvidencePreparation)) == 3
        assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == ledger_before


def test_manual_service_waits_exclusive_gate_and_rechecks_epoch_and_session():
    """No proposal/audit may be written before gate acquisition + freshness check."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import text

    from app.db import engine
    from app.leave_manual_opening import prepare_manual_opening_group
    from app.models import AdminSession
    from app.restore_lock import RESTORE_LOCK_KEY, read_restore_epoch

    with SessionLocal() as db:
        actor = db.scalar(select(AdminAccount).where(AdminAccount.username=='synthetic-system_admin'))
        employee_id = db.scalar(select(Employee.id).where(Employee.code=='SYN-FULLPATH'))
        session_id = db.scalar(select(AdminSession.id).where(AdminSession.admin_account_id==actor.id))
        identity = dict(actor_id=actor.id, session_id=session_id, session_version=actor.session_version,
            expected_epoch=read_restore_epoch(db))
        before = db.scalar(select(func.count()).select_from(LeaveHistoricalEvidencePreparation))
        audits = db.scalar(select(func.count()).select_from(AuditLog))

    for change in ('epoch', 'session_version'):
        entered = Event()
        def worker(entered=entered):
            with SessionLocal.begin() as db:
                entered.set()
                with pytest.raises(ValueError):
                    prepare_manual_opening_group(db, employee_id=employee_id,
                        balances={'A':Decimal(3),'W':Decimal(4),'H':Decimal(5)},
                        effective_start=date(2026,6,1), reason='Synthetic stale write', **identity)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with engine.begin() as connection:
                connection.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key':RESTORE_LOCK_KEY})
                task = pool.submit(worker)
                assert entered.wait(2)
                # Wait until PostgreSQL proves the worker is waiting on the
                # exclusive advisory lock, not an arbitrary sleep or mock.
                import time
                deadline = time.monotonic()+2
                while time.monotonic()<deadline:
                    waiting = connection.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted"))
                    if waiting: break
                    time.sleep(.01)
                assert waiting and not task.done()
                assert connection.scalar(select(func.count()).select_from(LeaveHistoricalEvidencePreparation)) == before
                assert connection.scalar(select(func.count()).select_from(AuditLog)) == audits
                if change == 'epoch':
                    connection.execute(text("INSERT INTO system_settings (key,value,updated_by,updated_at) VALUES ('restore_epoch','synthetic1006','synthetic',now()) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value"))
                else:
                    connection.execute(text('UPDATE admin_accounts SET session_version=session_version+1 WHERE id=:id'), {'id':identity['actor_id']})
            task.result(timeout=4)
        with SessionLocal() as db:
            assert db.scalar(select(func.count()).select_from(LeaveHistoricalEvidencePreparation)) == before
            assert db.scalar(select(func.count()).select_from(AuditLog)) == audits
            assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == 0
            identity['expected_epoch'] = read_restore_epoch(db)


def test_simultaneous_group_retries_use_one_saved_selection():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.leave_manual_opening_groups import persist_manual_opening_group
    from app.models import AdminSession, LeaveManualOpeningGroup
    from app.restore_lock import read_restore_epoch
    with SessionLocal() as db:
        actor = db.scalar(select(AdminAccount).where(AdminAccount.username=='synthetic-system_admin'))
        employee_id = db.scalar(select(Employee.id).where(Employee.code=='SYN-FULLPATH'))
        session_id = db.scalar(select(AdminSession.id).where(AdminSession.admin_account_id==actor.id))
        identity = dict(actor_id=actor.id, session_id=session_id, session_version=actor.session_version,
            expected_epoch=read_restore_epoch(db))
        before_groups = db.scalar(select(func.count()).select_from(LeaveManualOpeningGroup))
        before_proposals = db.scalar(select(func.count()).select_from(LeaveHistoricalEvidencePreparation))
        before_audits = db.scalar(select(func.count()).select_from(AuditLog))
    ready = Barrier(2)
    def worker():
        with SessionLocal.begin() as db:
            ready.wait(timeout=3)
            group, _reviews = persist_manual_opening_group(db, employee_id=employee_id,
                balances={'A':Decimal(6),'W':Decimal(7),'H':Decimal(8)},
                effective_start=date(2026,7,1), reason='Synthetic concurrent same group', **identity)
            return group.id
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(worker), pool.submit(worker)
        assert a.result(timeout=6) == b.result(timeout=6)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(LeaveManualOpeningGroup)) == before_groups+1
        assert db.scalar(select(func.count()).select_from(LeaveHistoricalEvidencePreparation)) == before_proposals+3
        assert db.scalar(select(func.count()).select_from(AuditLog)) == before_audits+4
        assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction)) == 0


def test_real_main_saved_group_and_error_get_wait_for_restore_and_reauthenticate(monkeypatch):
    """Real main composition and signed sessions, no fake auth/CSRF/gate."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import time

    from sqlalchemy import text
    from app.db import engine
    from app.models import LeaveManualOpeningGroup
    from app.restore_lock import RESTORE_LOCK_KEY
    monkeypatch.setattr(main,'send_login_alert',lambda *a,**k:None)
    monkeypatch.setattr(main,'send_telegram_login_event',lambda *a,**k:None)
    with SessionLocal() as db:
        group_id=db.scalar(select(LeaveManualOpeningGroup.id).order_by(LeaveManualOpeningGroup.created_at.desc()))
    with TestClient(main.app) as client:
        login=client.post('/login',data={'csrf_token':token(client.get('/login')),
            'username':'synthetic-system_admin','password':'Synthetic-Only-Login-1004!'},follow_redirects=False)
        assert login.status_code==303
        for path, revoke in [('/leave/opening/groups/'+group_id,False),('/leave/opening?error=1',True)]:
            entered=Event()
            def request(entered=entered,path=path):
                entered.set()
                return client.get(path)
            with ThreadPoolExecutor(max_workers=1) as pool:
                with engine.begin() as lock:
                    lock.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':RESTORE_LOCK_KEY})
                    future=pool.submit(request)
                    assert entered.wait(2)
                    time.sleep(.1)
                    assert not future.done()
                    if revoke:
                        lock.execute(text("UPDATE admin_accounts SET role='finance' WHERE username='synthetic-system_admin'"))
                response=future.result(timeout=4)
            assert response.status_code==(403 if revoke else 200)
            if revoke:
                assert 'SYN-FULLPATH' not in response.text


def test_concurrent_confirmed_source_retries_one_revision_audit_no_ledger():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from app.models import AdminSession,LeaveOpeningSelection
    from app.restore_lock import read_restore_epoch
    from app.leave_manual_opening_groups import persist_manual_opening_group
    from app.leave_opening_source import review_opening_source,confirm_opening_source
    from app.leave_policy_store import OWNER_DEFAULT_POLICY,persist_policy_version
    with SessionLocal.begin() as db:
        actor=AdminAccount(username='synthetic-confirm-concurrency',password_hash='synthetic-not-login',
            role='system_admin',active=True,must_change_password=False)
        db.add(actor);db.flush()
        login=AdminSession(id='synthetic-confirm-concurrency-session',admin_account_id=actor.id)
        db.add(login);db.flush()
        identity=dict(actor_id=actor.id,session_id=login.id,session_version=actor.session_version,
            expected_epoch=read_restore_epoch(db))
        employee_id=db.scalar(select(Employee.id).where(Employee.code=='SYN-FULLPATH'))
        group,_=persist_manual_opening_group(db,employee_id=employee_id,
            balances={'A':Decimal(12),'W':Decimal(2),'H':Decimal(1)},
            effective_start=date(2026,7,1),reason='Synthetic concurrent confirmation',**identity)
        policy=persist_policy_version(db,policy=OWNER_DEFAULT_POLICY,reason='Synthetic explicit policy',**identity)
        review=review_opening_source(db,group_id=group.id,policy_id=policy.id,**identity)
        args=dict(group_id=group.id,policy_id=policy.id,expected_previous_id=review['expected_previous_id'],
            reviewed_source_sha256=review['source_sha256'],reason='Synthetic concurrency',human_confirmed=True,**identity)
        count_before=db.scalar(select(func.count()).select_from(LeaveOpeningSelection))
        audit_before=db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='leave_opening_source_confirmed'))
    barrier=Barrier(2)
    def worker():
        with SessionLocal.begin() as db:
            barrier.wait(timeout=3)
            return confirm_opening_source(db,**args).id
    with ThreadPoolExecutor(max_workers=2) as pool:
        a,b=pool.submit(worker),pool.submit(worker)
        assert a.result(timeout=6)==b.result(timeout=6)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(LeaveOpeningSelection))==count_before+1
        assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action=='leave_opening_source_confirmed'))==audit_before+1
        assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==0


def test_policy_prepare_confirm_wait_exclusive_restore_lock_then_refuse_stale_identity():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import time
    from sqlalchemy import text
    from app.db import engine
    from app.models import AdminSession,LeavePolicyEditDraft,LeavePolicyVersion
    from app.restore_lock import RESTORE_LOCK_KEY,read_restore_epoch
    from app.leave_policy_edit import prepare_policy_edit,confirm_policy_edit
    from app.leave_policy_store import persist_policy_version,OWNER_DEFAULT_POLICY
    from tests.test_leave_policy_edit import fields
    with SessionLocal.begin() as db:
        actor=db.scalar(select(AdminAccount).where(AdminAccount.username=='synthetic-system_admin'))
        actor.role='system_admin';db.flush()
        session=db.scalar(select(AdminSession).where(AdminSession.admin_account_id==actor.id))
        auth=dict(actor_id=actor.id,session_id=session.id,session_version=actor.session_version,expected_epoch=read_restore_epoch(db))
        base=persist_policy_version(db,policy=OWNER_DEFAULT_POLICY,reason='Synthetic wait',**auth)
        base_id=base.id
        draft=prepare_policy_edit(db,base_policy_id=base_id,fields=fields(),reason='Synthetic wait',**auth)
        draft_id=draft.id;digest=draft.review_sha256
    for operation in ('prepare','confirm'):
        for change in ('epoch','session'):
            entered=Event()
            def worker():
                with SessionLocal.begin() as db:
                    entered.set()
                    with pytest.raises((ValueError,main.HTTPException)):
                        if operation=='prepare':prepare_policy_edit(db,base_policy_id=base_id,fields=fields(),reason='Synthetic stale',**auth)
                        else:confirm_policy_edit(db,draft_id=draft_id,reviewed_sha256=digest,human_confirmed=True,**auth)
            with ThreadPoolExecutor(max_workers=1) as pool:
                with engine.begin() as connection:
                    connection.execute(text('SELECT pg_advisory_xact_lock(:key)'),{'key':RESTORE_LOCK_KEY})
                    before=[connection.scalar(select(func.count()).select_from(m)) for m in (LeavePolicyEditDraft,LeavePolicyVersion,AuditLog)]
                    task=pool.submit(worker);assert entered.wait(2)
                    deadline=time.monotonic()+2;waiting=0
                    while time.monotonic()<deadline:
                        waiting=connection.scalar(text("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted"))
                        if waiting:break
                        time.sleep(.01)
                    assert waiting and not task.done()
                    if change=='epoch':connection.execute(text("UPDATE system_settings SET value='synthetic-policy-stale' WHERE key='restore_epoch'"))
                    else:connection.execute(text('UPDATE admin_accounts SET session_version=session_version+1 WHERE id=:id'),{'id':auth['actor_id']})
                task.result(timeout=4)
            with engine.begin() as connection:
                assert [connection.scalar(select(func.count()).select_from(m)) for m in (LeavePolicyEditDraft,LeavePolicyVersion,AuditLog)]==before
                if change=='epoch':connection.execute(text("UPDATE system_settings SET value=:epoch WHERE key='restore_epoch'"),{'epoch':auth['expected_epoch']})
                else:connection.execute(text('UPDATE admin_accounts SET session_version=:version WHERE id=:id'),{'version':auth['session_version'],'id':auth['actor_id']})


def test_policy_impact_rereads_draft_after_actual_table_lock_wait():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    import time
    from sqlalchemy import text
    from app.db import engine
    from app.models import AdminSession,LeavePolicyEditDraft,LeavePolicyVersion
    from app.restore_lock import read_restore_epoch
    from app.leave_policy_edit import prepare_policy_edit
    from app.leave_policy_impact import preview_earned_policy_impact
    from app.leave_policy_store import persist_policy_version,OWNER_DEFAULT_POLICY
    from tests.test_leave_policy_edit import fields
    with SessionLocal.begin() as db:
        actor=db.scalar(select(AdminAccount).where(AdminAccount.username=='synthetic-system_admin'))
        actor.role='system_admin';db.flush()
        session=db.scalar(select(AdminSession).where(AdminSession.admin_account_id==actor.id))
        auth=dict(actor_id=actor.id,session_id=session.id,session_version=actor.session_version,expected_epoch=read_restore_epoch(db))
        base=persist_policy_version(db,policy=OWNER_DEFAULT_POLICY,reason='Synthetic lockwait',**auth)
        draft=prepare_policy_edit(db,base_policy_id=base.id,fields=fields(),reason='Synthetic lockwait',**auth)
        draft_id=draft.id;digest=draft.review_sha256
    entered=Event()
    def worker():
        with SessionLocal.begin() as db:
            entered.set()
            with pytest.raises(ValueError,match='owner-bound'):
                preview_earned_policy_impact(db,draft_id=draft_id,reviewed_sha256=digest,selection_ids=('a'*64,),
                    department_id=1,month_start=date(2026,2,1),current_insurance_years={},age_evaluation_dates={},**auth)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with engine.begin() as conn:
            conn.execute(text('LOCK TABLE leave_policy_edit_drafts IN ACCESS EXCLUSIVE MODE'))
            before=[conn.scalar(select(func.count()).select_from(m)) for m in (LeavePolicyEditDraft,LeavePolicyVersion,AuditLog)]
            task=pool.submit(worker);assert entered.wait(2)
            deadline=time.monotonic()+2;waiting=0
            while time.monotonic()<deadline:
                waiting=conn.scalar(text("SELECT count(*) FROM pg_locks l JOIN pg_class c ON c.oid=l.relation WHERE c.relname='leave_policy_edit_drafts' AND NOT l.granted"))
                if waiting:break
                time.sleep(.01)
            assert waiting and not task.done()
            conn.execute(text("UPDATE leave_policy_edit_drafts SET reason='Synthetic changed while blocked' WHERE id=:id"),{'id':draft_id})
        task.result(timeout=4)
    with SessionLocal() as db:
        assert [db.scalar(select(func.count()).select_from(m)) for m in (LeavePolicyEditDraft,LeavePolicyVersion,AuditLog)]==before


def test_pay_basis_real_main_saved_provenance_no_financial_effects(monkeypatch):
    from app.models import PayrollRun,PayrollItem
    from sqlalchemy import inspect
    monkeypatch.setattr(main,'send_login_alert',lambda *a,**k:None)
    monkeypatch.setattr(main,'send_telegram_login_event',lambda *a,**k:None)
    with SessionLocal.begin() as db:
        actor=db.scalar(select(AdminAccount).where(AdminAccount.username=='synthetic-system_admin'))
        actor.role='system_admin'
        employee=db.scalar(select(Employee).where(Employee.code=='SYN-FULLPATH'))
        run=PayrollRun(period_start=date(2026,2,1),period_end=date(2026,2,28),pay_date=date(2026,3,1),status='draft',created_by='synthetic')
        db.add(run);db.flush()
        item=PayrollItem(payroll_run_id=run.id,employee_id=employee.id,employee_code_snapshot=employee.code,employee_name_snapshot='Synthetic saved pay name',department_name_snapshot='Synthetic',position_snapshot='Synthetic',salary_basis=Decimal(6000),base_salary=Decimal(3000),daily_hours=Decimal(8),performance_bonus=Decimal(3000),net_pay=Decimal(0))
        db.add(item);db.flush();item_id=item.id;run_id=run.id
    with TestClient(main.app) as client:
        url='/payroll/items/'+str(item_id)+'/basis-preview'
        assert client.get(url).status_code==401
        assert client.post('/login',data={'csrf_token':token(client.get('/login')),'username':'synthetic-system_admin','password':'Synthetic-Only-Login-1004!'},follow_redirects=False).status_code==303
        page=client.get(url);csrf=token(page)
        assert page.status_code==200 and 'Synthetic saved pay name' in page.text
        assert 'قد تكون صفرًا افتراضيًا' in page.text
        fields={'csrf_token':csrf,'cashout_days':'3','manual_overtime_hours':'2','overtime_divisor_hours':'8'}
        assert client.post(url,data={k:v for k,v in fields.items() if k!='csrf_token'}).status_code==403
        assert client.post(url,data={**fields,'extra':'1'},follow_redirects=False).headers['location']=='/payroll?error=item'
        def state():
            with SessionLocal() as db:
                return tuple(tuple(getattr(row,c.key) for c in inspect(m).columns) for m,row in [(PayrollItem,db.get(PayrollItem,item_id)),(PayrollRun,db.get(PayrollRun,run_id))]),db.scalar(select(func.count()).select_from(AuditLog))
        before=state()
        result=client.post(url,data=fields)
        assert result.status_code==200 and result.headers['cache-control']=='no-store'
        assert '900' in result.text and '75' in result.text
        assert 'ليست رصيدًا معتمدًا' in result.text and state()==before
        client.cookies.clear()
        assert client.post('/login',data={'csrf_token':token(client.get('/login')),'username':'synthetic-finance','password':'Synthetic-Only-Login-1004!'},follow_redirects=False).status_code==303
        assert client.get(url).status_code==403


def test_ot_day_real_main_no_award_or_source_write(monkeypatch):
    from datetime import datetime
    from app.models import PayrollRun,PayrollItem,AttendanceDailyRecord
    from sqlalchemy import inspect
    monkeypatch.setattr(main,'send_login_alert',lambda *a,**k:None)
    monkeypatch.setattr(main,'send_telegram_login_event',lambda *a,**k:None)
    with SessionLocal.begin() as db:
        department=Department(name='Synthetic standalone OT',active=True)
        db.add(department);db.flush()
        employee=Employee(code='SYN-STANDALONE-OT',full_name='Synthetic OT',department_id=department.id,position='Synthetic',hire_date=date(2025,1,1),status='active',basic_salary=Decimal(6000),daily_hours=Decimal(8))
        db.add(employee)
        for role in ('system_admin','finance'):
            db.add(AdminAccount(username='synthetic-ot-'+role,password_hash=hash_password('Synthetic-Only-Login-1004!'),role=role,active=True,must_change_password=False))
        db.flush()
        run=PayrollRun(period_start=date(2026,2,1),period_end=date(2026,2,28),pay_date=date(2026,3,1),status='draft',created_by='synthetic')
        db.add(run);db.flush()
        item=PayrollItem(payroll_run_id=run.id,employee_id=employee.id,employee_code_snapshot=employee.code,employee_name_snapshot='Synthetic standalone OT',department_name_snapshot='Synthetic',position_snapshot='Synthetic',salary_basis=Decimal(6000),base_salary=Decimal(3000),daily_hours=Decimal(8),performance_bonus=Decimal(3000),net_pay=Decimal(0))
        db.add(item);db.flush()
        item_id=item.id;run_id=item.payroll_run_id
        row=AttendanceDailyRecord(employee_id=item.employee_id,work_date=date(2026,2,2),status='approved',scheduled_start_at=datetime(2026,2,2,9),scheduled_end_at=datetime(2026,2,2,17),approved_clock_in=datetime(2026,2,2,9),approved_clock_out=datetime(2026,2,2,19),worked_minutes=600)
        db.add(row);db.flush();row_id=row.id
    with TestClient(main.app) as client:
        url=f'/payroll/items/{item_id}/overtime-day-preview'
        assert client.get(url).status_code==401
        assert client.post('/login',data={'csrf_token':token(client.get('/login')),'username':'synthetic-ot-system_admin','password':'Synthetic-Only-Login-1004!'},follow_redirects=False).status_code==303
        page=client.get(url);csrf=token(page)
        assert 'overtime-day-comparison' not in page.text
        fields=dict(csrf_token=csrf,work_date='2026-02-02',manual_hours='3',rate_divisor_hours='8',override_reason='<script>Synthetic</script>')
        assert client.post(url,data={k:v for k,v in fields.items() if k!='csrf_token'}).status_code==403
        assert client.post(url,data={**fields,'employee_id':'2'},follow_redirects=False).status_code==303
        assert client.post(url,data={**fields,'work_date':'2026-03-02'},follow_redirects=False).status_code==303
        def state():
            with SessionLocal() as db:
                return tuple(tuple(getattr(row,c.key) for c in inspect(m).columns) for m,row in [(PayrollItem,db.get(PayrollItem,item_id)),(PayrollRun,db.get(PayrollRun,run_id)),(AttendanceDailyRecord,db.get(AttendanceDailyRecord,row_id))]),db.scalar(select(func.count()).select_from(AuditLog))
        for key,value in [('work_date','2026-02-30'),('work_date','٢026-02-02'),('rate_divisor_hours','10'),('rate_divisor_hours','08'),('manual_hours','NaN'),('manual_hours','-1'),('manual_hours','1e4'),('override_reason','x'*2001)]:
            invalid=client.post(url,data={**fields,key:value},follow_redirects=False)
            assert invalid.status_code==303
        from urllib.parse import urlencode
        duplicate=urlencode(list(fields.items())+[('manual_hours','4')])
        assert client.post(url,content=duplicate,headers={'content-type':'application/x-www-form-urlencoded'},follow_redirects=False).status_code==303
        unknown=client.post(url,data={**fields,'work_date':'2026-02-03'})
        assert unknown.status_code==200 and 'غير معروف، ليس صفرًا' in unknown.text
        assert re.search(r'ساعات زيادة مدة العمل، ليست استحقاقًا</span>:\s*<span>غير معروف، ليس صفرًا</span>',unknown.text)
        before=state()
        response=client.post(url,data=fields)
        assert response.status_code==200 and response.headers['cache-control']=='no-store'
        assert '112.50' in response.text and '37.50' in response.text and 'ما زالت الزيادة' in response.text
        assert '<script>Synthetic</script>' not in response.text and '&lt;script&gt;Synthetic' in response.text
        assert state()==before
        for lang in ('en','zh'):
            client.cookies.set('my_hr_language',lang)
            localized=client.post(url,data=fields)
            content=localized.text.split('<main class="content">',1)[1].split('<footer>',1)[0]
            assert re.search(r'[\u0600-\u06ff]',content) is None
        client.cookies.clear()
        assert client.post('/login',data={'csrf_token':token(client.get('/login')),'username':'synthetic-ot-finance','password':'Synthetic-Only-Login-1004!'},follow_redirects=False).status_code==303
        assert client.get(url).status_code==403
        assert client.post(url,data={**fields,'csrf_token':token(client.get('/payroll'))},follow_redirects=False).status_code==403
