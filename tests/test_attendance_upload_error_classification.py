from contextlib import nullcontext
from datetime import date
from types import SimpleNamespace
import pytest
from sqlalchemy.exc import IntegrityError
from app.attendance_workflow import persist_attendance_import

class DB:
    def scalar(self,query):return None
    def add(self,row):pass
    def begin_nested(self):return nullcontext()
    def flush(self):raise self.error

@pytest.mark.parametrize('state,constraint,converted',[
    ('23505','uq_attendance_import_source_hash',True),
    ('23505','other_unique_constraint',False),
    ('23514','uq_attendance_import_source_hash',False),
    ('23503','attendance_import_batches_uploaded_by_fkey',False),
])
def test_only_exact_source_hash_unique_violation_is_clean_duplicate(state,constraint,converted):
    db=DB(); original=Exception('synthetic db failure');original.sqlstate=state;original.diag=SimpleNamespace(constraint_name=constraint)
    db.error=IntegrityError('synthetic',{},original)
    parsed=dict(source_type='program_report',period_start=date(2026,11,1),period_end=date(2026,11,30),events=[],issues=[])
    with pytest.raises(ValueError if converted else IntegrityError) as caught:
        persist_attendance_import(db,file_bytes=b'synthetic',filename='synthetic.xlsx',parsed=parsed,actor='synthetic')
    if converted:assert str(caught.value)=='attendance_source_file_already_imported' and caught.value.__cause__ is db.error
    else:assert caught.value is db.error
