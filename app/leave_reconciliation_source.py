"""Validated saved comparison to selected-source adapter, no writes."""
from hashlib import sha256
import re
from sqlalchemy import select
from app.models import Employee,LeaveOpeningSelection,DepartmentFingerprintPreparation,DepartmentWorkbookPreparation,DepartmentPreparationContract
from app.department_contract import decode_selection
from app.department_workbook_guard import bounded_workbook_operation
from app.shift_day_store import read_saved_shift_days


@bounded_workbook_operation
def reconciliation_source(db,comparison_id,employee_id,auth):
    if re.fullmatch('[0-9a-f]{64}',comparison_id) is None or not 0<employee_id<=2147483647:raise ValueError('Exact source required.')
    raw=read_saved_shift_days(db,comparison_id=comparison_id,employee_id=employee_id,**auth)
    comparison=db.get(DepartmentFingerprintPreparation,comparison_id,populate_existing=True)
    returned=db.get(DepartmentWorkbookPreparation,comparison.return_id,populate_existing=True)
    contract=db.get(DepartmentPreparationContract,returned.export_id,populate_existing=True)
    if contract is None or sha256(contract.selection_json.encode()).hexdigest()!=contract.selection_sha256:raise ValueError('Confirmed source contract required.')
    selection=decode_selection(contract.selection_json)
    selected=db.scalars(select(LeaveOpeningSelection).where(LeaveOpeningSelection.id.in_(selection['selection_ids']),LeaveOpeningSelection.employee_id==employee_id)).all()
    if len(selected)!=1:raise ValueError('One exact selected employee source required.')
    selection['selection_ids']=(selected[0].id,)
    selection['current_insurance_years']={k:v for k,v in selection['current_insurance_years'].items() if k[0]==employee_id}
    employee=db.get(Employee,employee_id,populate_existing=True)
    return selection,dict(code=employee.code,name=employee.full_name),raw
