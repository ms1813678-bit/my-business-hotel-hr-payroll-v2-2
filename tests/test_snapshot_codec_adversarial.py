import json
from decimal import Decimal
from datetime import datetime,timezone,timedelta,tzinfo
import pytest
import app.snapshot_codec as c
U='91a3ef61-34cb-4eb8-9550-f592477f81a6'
def raw(tag,value):
 return json.dumps({'entity':'employees','uid':U,'values':{'x':{'t':tag,'v':value}},'version':1},sort_keys=True,separators=(',',':')).encode()
@pytest.mark.parametrize('v',[10**5000,-10**5000],ids=['positive','negative'])
def test_huge_input_integer_controlled(v):
 with pytest.raises(c.SnapshotCodecError):c.encode_snapshot('employees',U,{'x':v})
@pytest.mark.parametrize('exp',['9'*5000,'-'+'9'*5000],ids=['positive','negative'])
def test_huge_encoded_decimal_exponent_controlled(exp):
 with pytest.raises(c.SnapshotCodecError):c.decode_snapshot(raw('decimal','0:1:'+exp))
@pytest.mark.parametrize('offset',[timedelta(microseconds=1),-timedelta(microseconds=1),timedelta(microseconds=999999),timedelta(seconds=30,microseconds=1),timedelta(hours=23,minutes=59,seconds=59,microseconds=999999)])
def test_all_aware_subsecond_offsets_roundtrip(offset):
 d=datetime(2026,1,1,12,34,56,987654,tzinfo=timezone(offset))
 b=c.encode_snapshot('employees',U,{'d':d});back=c.decode_snapshot(b)
 assert back.values['d'].utcoffset()==offset
 assert c.encode_snapshot(back.entity,back.uid,back.values)==b
class BadTZ(tzinfo):
 def utcoffset(self,dt):return timedelta(hours=24)
def test_invalid_custom_timezone_controlled():
 with pytest.raises(c.SnapshotCodecError):c.encode_snapshot('employees',U,{'x':datetime(2026,1,1,tzinfo=BadTZ())})
@pytest.mark.parametrize('s',['2026-01-01T00:00:00+00:60','2026-01-01T00:00:00+00:00:60','2026-01-01T00:00:00+00:00:00.000000'])
def test_noncanonical_offsets_reject(s):
 with pytest.raises(c.SnapshotCodecError):c.decode_snapshot(raw('datetime',s))
@pytest.mark.parametrize('tag,v',[('int','1\n'),('decimal','0:1:0\n'),('decimal','0:1:'+'0'*5000)])
def test_numeric_suffix_and_noncanonical_exponent_reject(tag,v):
 with pytest.raises(c.SnapshotCodecError):c.decode_snapshot(raw(tag,v))
def test_actual_supported_type_values_and_digest_distinction():
 vals={'basic_salary':Decimal('1234.50'),'hire_date':__import__('datetime').date(2026,1,1),'created_at':datetime(2026,1,1,tzinfo=timezone.utc),'active':True,'full_name':'أحمد 年假','department_uid':U}
 back=c.decode_snapshot(c.encode_snapshot('employees',U,vals))
 assert back.values==vals
 assert c.digest_snapshot('employees',U,{'x':Decimal('1.00')})!=c.digest_snapshot('employees',U,{'x':'1.00'})
