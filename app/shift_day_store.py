"""Read-only saved department/fingerprint day-list, no registration authority."""
import json,re
from datetime import date
from hashlib import sha256
from dataclasses import dataclass
from sqlalchemy import text
from app.department_workbook_guard import bounded_workbook_operation
from app.leave_policy_store import policy_read_guard
from app.department_workbook_store import decode_receipt_evidence
from app.department_manifest import decode_manifest
from app.attendance_import import normalize
from app.models import DepartmentFingerprintPreparation,DepartmentWorkbookPreparation,DepartmentWorkbookExport,Employee,Department


@dataclass(frozen=True)
class SavedShiftDays:
    comparison_id:str
    employee_id:int
    employee_code:str
    employee_name:str
    days:tuple
    fingerprint_bytes:bytes
    filename_hint:str
    source_digest:str


@bounded_workbook_operation
def read_saved_shift_days(db,*,comparison_id,employee_id,**identity):
    if db.new or db.dirty or db.deleted:raise ValueError('Clean read-only transaction required.')
    policy_read_guard(db,**identity)
    if not isinstance(comparison_id,str) or re.fullmatch('[0-9a-f]{64}',comparison_id) is None or type(employee_id) is not int or employee_id<1:
        raise ValueError('Exact comparison and employee required.')
    db.execute(text('LOCK TABLE department_fingerprint_preparations,department_workbook_preparations,department_workbook_exports IN SHARE MODE'))
    policy_read_guard(db,**identity)
    row=db.get(DepartmentFingerprintPreparation,comparison_id,populate_existing=True)
    receipt=db.get(DepartmentWorkbookPreparation,row.return_id,populate_existing=True) if row else None
    export=db.get(DepartmentWorkbookExport,receipt.export_id,populate_existing=True) if receipt else None
    if not row or not receipt or not export or export.invalidated_at is not None or row.restore_epoch!=identity['expected_epoch'] or export.restore_epoch!=identity['expected_epoch']:
        raise ValueError('Exact current-epoch saved sources required.')
    for content,digest in ((row.review_json,row.review_sha256),(receipt.evidence_json,receipt.evidence_sha256),(export.manifest_json,export.manifest_sha256)):
        if not digest or sha256(content.encode()).hexdigest()!=digest:raise ValueError('Saved source integrity mismatch.')
    raw=row.fingerprint_bytes
    if not raw or sha256(raw).hexdigest()!=row.fingerprint_sha256:raise ValueError('Fingerprint source integrity mismatch.')
    if len(row.review_json.encode())>25000000:raise ValueError('Bounded saved comparison required.')
    try:review=json.loads(row.review_json)
    except (ValueError,TypeError) as exc:raise ValueError('Malformed saved comparison.') from exc
    if (type(review) is not dict or review.get('version')!=2 or review.get('export_id')!=export.id
            or review.get('department_sha256')!=receipt.return_sha256 or review.get('fingerprint_sha256')!=row.fingerprint_sha256):
        raise ValueError('Saved comparison/export/returned source binding mismatch.')
    manifest=decode_manifest(export.manifest_json)
    employee=next((v for v in manifest.employees if v.employee_id==employee_id),None)
    if employee is None:raise ValueError('Employee outside saved export.')
    db.execute(text('LOCK TABLE employees,departments IN SHARE MODE'))
    current=db.get(Employee,employee_id,populate_existing=True)
    department=db.get(Department,current.department_id,populate_existing=True) if current else None
    if not current or not department or current.code!=employee.code or current.full_name!=employee.name or department.name!=manifest.department:
        raise ValueError('Saved export/live employee-department identity changed.')
    bindings=review.get('alias_bindings')
    if (type(bindings) is not list or any(type(v) is not list or len(v)!=3 or not isinstance(v[0],str) or not isinstance(v[1],str) or type(v[2]) is not int for v in bindings)
        or sha256(json.dumps(bindings,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()!=review.get('alias_bindings_sha256')):
        raise ValueError('Exact saved comparison alias bindings required.')
    saved_aliases=sorted(tuple(v) for v in bindings if v[2]==employee_id)
    current_aliases=sorted({(alias,normalize(alias),employee_id) for alias in (current.code,current.fingerprint_id) if alias})
    if saved_aliases!=current_aliases:raise ValueError('Saved/live employee alias identity changed.')
    evidence=decode_receipt_evidence(receipt.evidence_json)
    review_days=review.get('days')
    if (not isinstance(review_days,list) or len(review_days)!=len(evidence)
        or any(type(v) is not dict for v in review_days)):
        raise ValueError('Saved comparison/returned days mismatch.')
    expected=[dict(employee_id=v['employee_id'],date=v['date'],department_code=v['code'],sheet=v['sheet'],cell=v['cell']) for v in evidence]
    actual=[{k:v.get(k) for k in ('employee_id','date','department_code','sheet','cell')} for v in review_days]
    if expected!=actual:raise ValueError('Saved comparison/returned days mismatch.')
    selected=tuple(v for v in evidence if v['employee_id']==employee_id)
    if not selected or len(selected)>31 or any(date.fromisoformat(v['date']).replace(day=1)!=manifest.month_start for v in selected) or len({v['date'] for v in selected})!=len(selected):raise ValueError('Unique saved employee month days required.')
    # Original filename was not retained by the legacy preparation schema.
    # Only a format hint from original magic bytes is used, never shown as filename.
    hint='source.xlsx' if raw.startswith(b'PK') else 'source.xls' if raw.startswith(bytes.fromhex('d0cf11e0a1b11ae1')) else 'source.csv'
    digest=sha256(json.dumps(dict(comparison_id=row.id,return_id=receipt.id,export_id=export.id,return_sha256=receipt.return_sha256,restore_epoch=row.restore_epoch,comparison=row.review_sha256,receipt=receipt.evidence_sha256,manifest=export.manifest_sha256,
        fingerprint=row.fingerprint_sha256,employee_id=employee_id),sort_keys=True).encode()).hexdigest()
    # Employee manifest uses stable export-time fields, not current payroll.
    return SavedShiftDays(comparison_id,employee_id,employee.code,employee.name,selected,raw,hint,digest)
