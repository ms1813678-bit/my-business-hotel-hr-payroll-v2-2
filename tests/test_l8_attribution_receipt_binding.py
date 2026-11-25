import json
from sqlalchemy import text
from app.leave_year_attribution_store import verify_year_attribution, digest
from app.models import LeaveYearAttribution
from tests.test_leave_year_attribution_store import source, apply  # noqa
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
import pytest


@pytest.mark.parametrize('field', ['plan', 'reviewed_digest', 'actor_id'])
def test_receipt_request_must_bind_to_result_not_only_digest(source, field):
    """Forge request_json (triggers bypassed as a DBA/restore could) with a self-consistent request_digest.
    The receipt must still refuse: its request must reference this exact result."""
    db, row, args = source
    apply(db, args)
    receipt = db.query(LeaveYearAttribution).one()
    request = json.loads(receipt.request_json)
    request[field] = {'plan': {'forged': 1}, 'reviewed_digest': 'f' * 64, 'actor_id': request['actor_id'] + 999}[field]
    db.execute(text("SET LOCAL session_replication_role = replica"))
    db.execute(text('UPDATE leave_year_attributions SET request_json=:j, request_digest=:d'),
               dict(j=json.dumps(request, sort_keys=True, separators=(',', ':')), d=digest(request)))
    db.execute(text("SET LOCAL session_replication_role = origin"))
    db.expire_all()
    with pytest.raises(ValueError):
        verify_year_attribution(db, db.query(LeaveYearAttribution).one())
