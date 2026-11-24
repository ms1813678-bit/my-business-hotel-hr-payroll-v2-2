from datetime import datetime
import subprocess
import pytest
from app.fingerprint_bounded_parse import parse_operational_fingerprint


def test_actual_worker_preserves_global_duplicates_unknowns():
    raw=b'ID,Date,Time\nOTHER,2026-11-01,09:00\nSYN,2026-11-01,08:00 08:00 16:00\n'
    result=parse_operational_fingerprint(raw,filename='synthetic.csv',verified_aliases={'SYN':1})
    assert result.original_bytes==raw and len(result.occurrences)==4
    assert result.occurrences[0]['employee_id'] is None
    assert result.occurrences[1]['punch_at']==result.occurrences[2]['punch_at']==datetime(2026,11,1,8)


def test_timeout_controlled_and_no_shell_no_inherited_secrets(monkeypatch):
    def run(args,**kwargs):
        assert kwargs['timeout']==3 and 'shell' not in kwargs
        assert set(kwargs['env'])=={'PATH','PYTHONPATH','LANG'}
        raise subprocess.TimeoutExpired(args,3)
    monkeypatch.setattr(subprocess,'run',run)
    with pytest.raises(ValueError,match='budget'):parse_operational_fingerprint(b'synthetic',filename='synthetic.csv',verified_aliases={'SYN':1})


def test_worker_decode_failure_controlled():
    with pytest.raises(ValueError):parse_operational_fingerprint(b'invalid',filename='synthetic.xlsx',verified_aliases={'SYN':1})


@pytest.mark.parametrize('filename',['synthetic.csv','synthetic.xlsx'])
def test_direct_worker_equivalence_duplicates_unknown_unreadable_overnight(filename):
    from app.fingerprint_evidence import parse_fingerprint_evidence
    from io import BytesIO
    rows=[['ID','Date','Time'],['OTHER','2026-11-01','09:00'],['SYN','2026-11-01','23:00 23:00 bad'],['SYN','2026-11-02','07:00']]
    if filename.endswith('.csv'):raw=('\n'.join(','.join(v) for v in rows)+'\n').encode()
    else:
        from openpyxl import Workbook
        book=Workbook();sheet=book.active;sheet.title='Synthetic'
        for row in rows:sheet.append(row)
        out=BytesIO();book.save(out);raw=out.getvalue()
    args=dict(filename=filename,verified_aliases={'SYN':1})
    direct=parse_fingerprint_evidence(raw,**args);worker=parse_operational_fingerprint(raw,**args)
    assert worker==direct


@pytest.mark.parametrize('raw',[b'[]',b'{"status":"ok","status":"ok"}',b'{"status":"ok","period_start":null,"period_end":null,"occurrences":{},"issues":[]}',b'{"status":"ok","period_start":null,"period_end":null,"occurrences":[],"issues":[{"reason":"bad","value":{}}]}'])
def test_nonexecutable_bad_worker_schema_refusal(raw):
    from app.fingerprint_bounded_parse import _decode_worker
    with pytest.raises(ValueError):_decode_worker(raw)


def test_real_wall_budget_kills_waits_and_cleanly_refuses(monkeypatch):
    import app.fingerprint_bounded_parse as module
    real=subprocess.run
    seen={}
    def run(args,**kwargs):
        # Exercise real subprocess timeout kill/wait path, not injected exception.
        seen['stderr']=kwargs['stderr'];kwargs['timeout']=0.05
        return real([args[0],'-c','import time;time.sleep(10)'],**kwargs)
    monkeypatch.setattr(module.subprocess,'run',run)
    with pytest.raises(ValueError,match='budget'):parse_operational_fingerprint(b'ID,Date,Time\nSYN,2026-11-01,08:00\n',filename='synthetic.csv',verified_aliases={'SYN':1})
    assert seen['stderr']==subprocess.DEVNULL


def test_oversize_child_output_clean_refusal(monkeypatch):
    import app.fingerprint_bounded_parse as module
    def run(args,**kwargs):
        kwargs['stdout'].seek(4_000_001);kwargs['stdout'].write(b'x')
        return subprocess.CompletedProcess(args,0)
    monkeypatch.setattr(module.subprocess,'run',run)
    with pytest.raises(ValueError,match='budget'):parse_operational_fingerprint(b'synthetic',filename='synthetic.csv',verified_aliases={'SYN':1})


@pytest.mark.parametrize('value',[datetime(2026,11,1,8),0.5,None])
def test_xlsx_datetime_numeric_blank_equivalence(value):
    from io import BytesIO
    from openpyxl import Workbook
    from app.fingerprint_evidence import parse_fingerprint_evidence
    book=Workbook();sheet=book.active;sheet.title='Synthetic'
    sheet.append(['ID','Date','Time']);sheet.append(['SYN','2026-11-01',value]);sheet.append(['SYN','2026-11-02','07:00'])
    book.create_sheet('Blank');out=BytesIO();book.save(out)
    args=dict(filename='synthetic.xlsx',verified_aliases={'SYN':1})
    assert parse_operational_fingerprint(out.getvalue(),**args)==parse_fingerprint_evidence(out.getvalue(),**args)


@pytest.mark.parametrize('aliases',[{'SYN':True},{'SYN':2147483648},{'\ud800':1},{str(i)+'x'*1000:1 for i in range(600)}])
def test_bounded_valid_alias_inputs(aliases):
    with pytest.raises(ValueError):parse_operational_fingerprint(b'synthetic',filename='synthetic.csv',verified_aliases=aliases)
