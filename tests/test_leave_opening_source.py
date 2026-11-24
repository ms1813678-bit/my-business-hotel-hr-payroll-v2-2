from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.leave_manual_opening_groups import persist_manual_opening_group
from app.leave_opening_source import confirm_opening_source, read_confirmed_opening_source, review_opening_source
from app.leave_policy_store import OWNER_DEFAULT_POLICY, persist_policy_version
from app.models import LeaveBalanceTransaction, LeaveOpeningSelection
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401


def test_confirm_explicit_source_retry_replacement_stale_and_no_post(history_context):
    db,_account,auth,fact=history_context
    ledger=db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))
    policy=persist_policy_version(db,policy=OWNER_DEFAULT_POLICY,reason='Synthetic',**auth)
    def group(amount):
        return persist_manual_opening_group(db,employee_id=fact.employee_id,
            balances={'A':Decimal(amount),'W':Decimal(2),'H':Decimal(0)},effective_start=date(2026,5,17),
            reason='Synthetic',**auth)[0]
    first=group(8)
    review=review_opening_source(db,group_id=first.id,policy_id=policy.id,**auth)
    args=dict(group_id=first.id,policy_id=policy.id,expected_previous_id=review['expected_previous_id'],
        reviewed_source_sha256=review['source_sha256'],reason='Synthetic confirmation',human_confirmed=True,**auth)
    with pytest.raises(ValueError):
        confirm_opening_source(db,**{**args,'human_confirmed':False})
    row=confirm_opening_source(db,**args)
    assert confirm_opening_source(db,**args).id==row.id
    reviews,selected_policy,digest=read_confirmed_opening_source(db,selection_id=row.id,**auth)
    assert reviews[0]['days']=='8' and selected_policy==OWNER_DEFAULT_POLICY and digest==review['source_sha256']
    second=group(9)
    next_review=review_opening_source(db,group_id=second.id,policy_id=policy.id,**auth)
    replacement={**args,'group_id':second.id,'reviewed_source_sha256':next_review['source_sha256']}
    with pytest.raises(ValueError,match='selection changed'):
        confirm_opening_source(db,**replacement)
    replacement['expected_previous_id']=row.id
    newer=confirm_opening_source(db,**replacement)
    assert newer.previous_id==row.id and newer.revision==2
    with pytest.raises(ValueError,match='replaced'):
        read_confirmed_opening_source(db,selection_id=row.id,**auth)
    assert read_confirmed_opening_source(db,selection_id=newer.id,**auth)[0][0]['days']=='9'
    assert db.scalar(select(func.count()).select_from(LeaveOpeningSelection))==2
    assert db.scalar(select(func.count()).select_from(LeaveBalanceTransaction))==ledger
