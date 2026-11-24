"""Stored explicit export selection and returned workbook review, no fingerprint route."""
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from hashlib import sha256

from app.department_preparation_workflow import _confirmed_department_projection, prepare_confirmed_source_department_export
from app.department_workbook_guard import bounded_workbook_operation
from app.department_workbook_store import _guard, prepare_return
from app.models import DepartmentPreparationContract


def encode_selection(*, selection_ids, department_id, month_start, current_insurance_years, age_evaluation_dates):
    return json.dumps({'selection_ids':sorted(selection_ids),'department_id':department_id,
        'month_start':month_start.isoformat(),
        'current_insurance_years':sorted((i,m.isoformat(),format(v,'f') if v is not None else None)
            for (i,m),v in current_insurance_years.items()),
        'age_evaluation_dates':sorted((m.isoformat(),v.isoformat()) for m,v in age_evaluation_dates.items())},
        sort_keys=True,separators=(',',':'))


def decode_selection(text):
    if not isinstance(text,str) or len(text)>1000000:
        raise ValueError('Bounded explicit source contract required.')
    try:
        raw=json.loads(text)
        if type(raw) is not dict or set(raw)!={'selection_ids','department_id','month_start','current_insurance_years','age_evaluation_dates'}:
            raise ValueError('Exact source contract required.')
        if type(raw['department_id']) is not int or raw['department_id']<1:
            raise ValueError('Explicit department required.')
        ids=raw['selection_ids']
        if type(ids) is not list or not 1<=len(ids)<=300 or len(set(ids))!=len(ids) or any(
                not isinstance(i,str) or len(i)!=64 or any(c not in '0123456789abcdef' for c in i) for i in ids):
            raise ValueError('Exact confirmed source references required.')
        insurance={}
        for i,m,value in raw['current_insurance_years']:
            if type(i) is not int or i<1 or (i,date.fromisoformat(m)) in insurance:
                raise ValueError('Unique month insurance evidence required.')
            amount=Decimal(value) if value is not None and isinstance(value,str) and len(value)<=100 and re.fullmatch(r'\d+(?:\.\d+)?',value) else None
            if value is not None and (amount is None or not amount.is_finite() or amount<0):
                raise ValueError('Exact nonnegative insurance evidence required.')
            insurance[i,date.fromisoformat(m)]=amount
        ages={}
        for month,evaluated in raw['age_evaluation_dates']:
            if date.fromisoformat(month) in ages:
                raise ValueError('Unique age evaluation schedule required.')
            ages[date.fromisoformat(month)]=date.fromisoformat(evaluated)
        result=dict(selection_ids=tuple(ids),department_id=raw['department_id'],month_start=date.fromisoformat(raw['month_start']),
            current_insurance_years=insurance,age_evaluation_dates=ages)
        if encode_selection(**result)!=text:
            raise ValueError('Canonical source contract required.')
        return result
    except (KeyError,TypeError,InvalidOperation,AttributeError) as exc:
        raise ValueError('Malformed source contract.') from exc


@bounded_workbook_operation
def export_confirmed_department(db, **selection):
    identity={key:selection[key] for key in ('actor_id','session_id','session_version','expected_epoch')}
    inputs={key:value for key,value in selection.items() if key not in identity}
    text=encode_selection(**inputs)
    decoded=decode_selection(text)
    with db.begin_nested():
        result=prepare_confirmed_source_department_export(db,**decoded,**identity)
        db.add(DepartmentPreparationContract(export_id=result.export_id,selection_json=text,
            selection_sha256=sha256(text.encode()).hexdigest()))
        db.flush()
        return result


@bounded_workbook_operation
def review_confirmed_department_return(db, *, export_id, payload, **identity):
    """Current source recalculated from stored exact inputs, not caller digest."""
    _guard(db,**identity)
    contract=db.get(DepartmentPreparationContract,export_id,populate_existing=True)
    if contract is None or sha256(contract.selection_json.encode()).hexdigest()!=contract.selection_sha256:
        raise ValueError('Stored source contract required.')
    selection=decode_selection(contract.selection_json)
    projection,_department=_confirmed_department_projection(db,**selection,**identity)
    return prepare_return(db,export_id=export_id,payload=payload,
        current_source_digest=projection.source_digest,**identity)
