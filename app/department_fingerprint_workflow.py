"""Original-byte department/fingerprint comparison, review only, no registration."""
import json
import secrets
from datetime import UTC, date, datetime
from hashlib import sha256

from sqlalchemy import select

from app.department_contract import review_confirmed_department_return
from app.department_fingerprint_review import preview_department_fingerprint_review
from app.department_manifest import decode_manifest
from app.department_workbook import inspect_department_return
from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard
from app.fingerprint_evidence import parse_fingerprint_evidence
from app.models import AuditLog, DepartmentFingerprintPreparation, DepartmentWorkbookExport, Employee


def _json_value(value):
    if isinstance(value,(date,datetime)):
        return value.isoformat()
    raise TypeError('Unexpected comparison evidence type.')


@bounded_workbook_operation
def compare_department_fingerprint(db, *, export_id, department_bytes, fingerprint_bytes,
                                  fingerprint_filename, **identity):
    """Caller owns commit. Original bytes parsed here, never browser-built objects."""
    account=_guard(db,**identity)
    with db.begin_nested():
        receipt=review_confirmed_department_return(db,export_id=export_id,payload=department_bytes,**identity)
        export=db.get(DepartmentWorkbookExport,export_id,populate_existing=True)
        manifest=decode_manifest(export.manifest_json)
        ids=[employee.employee_id for employee in manifest.employees]
        # Projection reader already holds employee SHARE lock through this txn.
        from app.attendance_import import normalize
        employees=db.execute(select(Employee.id,Employee.code,Employee.fingerprint_id).order_by(Employee.id)).all()
        owners={}
        for employee in employees:
            for alias in (employee.code,employee.fingerprint_id):
                if alias:
                    owners.setdefault(normalize(alias),set()).add(employee.id)
        aliases={}
        for employee in employees:
            if employee.id not in ids:
                continue
            for alias in (employee.code,employee.fingerprint_id):
                if not alias:
                    continue
                if owners[normalize(alias)]!={employee.id}:
                    raise ValueError('Fingerprint identity collision requires review.')
                aliases[alias]=employee.id
        bindings=sorted((alias,normalize(alias),employee_id) for alias,employee_id in aliases.items())
        fingerprint=parse_fingerprint_evidence(fingerprint_bytes,filename=fingerprint_filename,verified_aliases=aliases)
        returned=inspect_department_return(department_bytes,manifest)
        preview=preview_department_fingerprint_review(department=returned,fingerprint=fingerprint)
        evidence={'version':2,'alias_bindings':bindings,
            'alias_bindings_sha256':sha256(json.dumps(bindings,ensure_ascii=False,separators=(',',':')).encode()).hexdigest(),'export_id':export_id,'department_sha256':preview.department_sha256,
            'fingerprint_sha256':preview.fingerprint_sha256,'grouping':'calendar_date_not_assigned_shift',
            'registration_allowed':False,'issues':list(preview.issues),
            'source_issues':[dict(i) for i in fingerprint.issues],
            'unmatched_occurrences':[dict(i) for i in preview.unmatched_occurrences],
            'days':[{'employee_id':d.employee_id,'date':d.department_work_date.isoformat(),
                'department_code':d.department_code,'sheet':d.source_sheet,'cell':d.source_cell,
                'state':d.review_state,'issues':list(d.issues),
                'punches':[dict(p) for p in d.calendar_date_occurrences],
                'unreadable':[dict(i) for i in d.unreadable_source_evidence]} for d in preview.days]}
        text=json.dumps(evidence,default=_json_value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
        if len(text.encode())>25000000:
            raise ValueError('Comparison exceeds bounded stored review size.')
        row=DepartmentFingerprintPreparation(id=secrets.token_hex(32),return_id=receipt.id,actor_id=account.id,
            fingerprint_bytes=fingerprint.original_bytes,fingerprint_sha256=fingerprint.source_sha256,
            review_json=text,review_sha256=sha256(text.encode()).hexdigest(),
            restore_epoch=identity['expected_epoch'],created_at=datetime.now(UTC))
        db.add(row);db.flush()
        db.add(AuditLog(actor=account.username,action='department_fingerprint_review_prepared',
            entity_type='department_fingerprint_preparation',entity_id=row.id,
            new_value=json.dumps({'return_id':receipt.id,'fingerprint_sha256':fingerprint.source_sha256,
                'review_sha256':row.review_sha256,'registration_allowed':False}),
            reason='Original-byte calendar-date evidence comparison only, no registration'))
        db.flush()
        return row
