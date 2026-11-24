from dataclasses import replace
import json
import pytest
from app.department_manifest import encode_manifest,decode_manifest
from tests.test_department_workbook import make_sample


def test_exact_roundtrip_and_canonical_encoding():
    _,m=make_sample()
    text=encode_manifest(m)
    assert decode_manifest(text)==m
    assert encode_manifest(decode_manifest(text))==text


def test_duplicate_keys_refused_before_dictionary_collapse():
    _,m=make_sample();text=encode_manifest(m)
    with pytest.raises(ValueError,match='Duplicate'):
        decode_manifest(text.replace('"version":1','"version":1,"version":1'))
    with pytest.raises(ValueError,match='Duplicate'):
        decode_manifest(text.replace('"employee_id":1','"employee_id":1,"employee_id":2',1))


def test_schema_identity_type_and_duplicate_domains_refused():
    _,m=make_sample();base=json.loads(encode_manifest(m))
    for mutate in (lambda x:x.update(extra=1),
                   lambda x:x.update(version=True),
                   lambda x:x['manifest']['employees'][0].update(employee_id=True),
                   lambda x:x['manifest']['employees'][1].update(employee_id=1),
                   lambda x:x['manifest'].update(immutable_digest='x'*64),
                   lambda x:x['manifest']['employees'][0]['annual'].update(carry=1.5)):
        x=json.loads(json.dumps(base));mutate(x)
        with pytest.raises(ValueError):decode_manifest(json.dumps(x))


def test_wrong_dates_duplicate_service_and_nonfinite_refused():
    _,m=make_sample();base=json.loads(encode_manifest(m))
    for field,value in [('month_start','2026-05-02'),('month_start','20260501')]:
        x=json.loads(json.dumps(base));x['manifest'][field]=value
        with pytest.raises(ValueError):decode_manifest(json.dumps(x))
    x=json.loads(json.dumps(base));x['manifest']['employees'][0]['employed_dates']=['2026-05-01']*2
    with pytest.raises(ValueError):decode_manifest(json.dumps(x))
    with pytest.raises(ValueError):decode_manifest('{"version":NaN,"manifest":{}}')
