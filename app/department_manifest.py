"""Strict server-side manifest codec, not workbook authority or a persisted grant.

No route, storage, signing secret, posting or disclosure. A future store must bind
these bytes to authenticated export/department/month/snapshot and current scope.
"""
from dataclasses import asdict
from datetime import date
from decimal import Decimal
import json
import re

from app.department_workbook import (
    BalanceSnapshot, DepartmentWorkbookEmployee, WorkbookManifest,
    _validate_manifest_domain,
)


def encode_manifest(manifest: WorkbookManifest) -> str:
    _validate_manifest_domain(manifest.department, manifest.month_start, manifest.employees)
    def convert(value):
        if isinstance(value, Decimal):return format(value, 'f')
        if type(value) is date:return value.isoformat()
        if isinstance(value, frozenset):return sorted(d.isoformat() for d in value)
        raise TypeError('Unexpected manifest field.')
    text = json.dumps({'version': 1, 'manifest': asdict(manifest)}, default=convert,
                      ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    decode_manifest(text)  # One schema/domain and output-size contract for both ends.
    return text


def _pairs(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('Duplicate manifest object key.')
        result[key]=value
    return result


def _fields(value, expected):
    if type(value) is not dict or set(value)!=set(expected):
        raise ValueError('Unexpected manifest schema.')


def decode_manifest(text: str) -> WorkbookManifest:
    if not isinstance(text,str) or len(text.encode())>2_000_000:
        raise ValueError('Bounded server manifest required.')
    # Bounded syntax depth outside JSON strings, before recursive parser allocation.
    depth=0; quoted=False; escaped=False
    for char in text:
        if quoted:
            if escaped:escaped=False
            elif char=='\\':escaped=True
            elif char=='"':quoted=False
        elif char=='"':quoted=True
        elif char in '[{':
            depth+=1
            if depth>20:raise ValueError('Manifest nesting exceeds bound.')
        elif char in ']}':depth-=1
    try:
        value=json.loads(text,object_pairs_hook=_pairs,parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite JSON.')))
    except (RecursionError, TypeError) as exc:
        raise ValueError('Invalid bounded manifest JSON.') from exc
    _fields(value,('version','manifest'))
    if type(value['version']) is not int or value['version']!=1:raise ValueError('Unsupported manifest version.')
    row=value['manifest']
    _fields(row,('department','month_start','employees','immutable_digest','export_sha256','structure_digest'))
    for field in ('immutable_digest','export_sha256','structure_digest'):
        if not isinstance(row[field],str) or not re.fullmatch('[0-9a-f]{64}',row[field]):raise ValueError('Invalid manifest digest.')
    if not isinstance(row['department'],str) or len(row['department'])>1000:raise ValueError('Invalid department label.')
    if type(row['employees']) is not list or not 1<=len(row['employees'])<=300:raise ValueError('Invalid manifest employees.')
    employees=[]
    def day(raw):
        if not isinstance(raw,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',raw):raise ValueError('Invalid manifest date.')
        return date.fromisoformat(raw)
    for e in row['employees']:
        expected=('employee_id','code','name','window_start','employed_dates','annual','holiday','weekly')
        _fields(e, expected + (('balance_usage_from',) if 'balance_usage_from' in e else ()))
        if any(not isinstance(e[f],str) or not e[f] or len(e[f])>1000 for f in ('code','name')):raise ValueError('Invalid manifest identity.')
        if type(e['employee_id']) is not int or e['employee_id']<1:raise ValueError('Invalid employee identity.')
        dates=e['employed_dates']
        if type(dates) is not list or len(dates)>31 or any(not isinstance(d,str) for d in dates) or len(dates)!=len(set(dates)):raise ValueError('Ambiguous service dates.')
        snapshots=[]
        for field in ('annual','holiday','weekly'):
            snap=e[field];_fields(snap,('carry','entitled_through_month_end','used_before_month'))
            numbers=[]
            for key in ('carry','entitled_through_month_end','used_before_month'):
                raw=snap[key]
                if not isinstance(raw,str) or len(raw)>1000 or not re.fullmatch(r'-?\d+(?:\.\d+)?',raw):raise ValueError('Exact decimal text required.')
                numbers.append(Decimal(raw))
            snapshots.append(BalanceSnapshot(*numbers))
        employees.append(DepartmentWorkbookEmployee(e['employee_id'],e['code'],e['name'],day(e['window_start']),frozenset(map(day,dates)),*snapshots, day(e['balance_usage_from']) if e.get('balance_usage_from') is not None else None))
    result=WorkbookManifest(row['department'],day(row['month_start']),tuple(employees),row['immutable_digest'],row['export_sha256'],row['structure_digest'])
    _validate_manifest_domain(result.department,result.month_start,result.employees)
    return result
