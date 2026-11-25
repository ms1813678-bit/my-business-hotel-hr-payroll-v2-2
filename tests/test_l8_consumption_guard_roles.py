from datetime import date
from fractions import Fraction
import pytest
from sqlalchemy import select
from app.models import AdminAccount, AdminSession
from app.leave_carry_consumption import review_carry_consumption, record_carry_consumption
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
from tests.test_l8_carry_consumption_adversarial import ready, P


def sel(base, day=3):
    return {**base, 'work_date': date(2026, 1, day), 'quantity': Fraction(1)}


@pytest.mark.parametrize('prior_year', P, indirect=True)
@pytest.mark.parametrize('mutate', ['finance', 'inactive', 'must_change', 'pending_reset', 'stale_version',
                                    'foreign_session', 'no_session', 'wrong_epoch'])
def test_review_and_record_refuse_every_non_current_editor(prior_year, mutate):
    db, base = ready(prior_year)
    good = review_carry_consumption(db, **sel(base))
    actor = db.get(AdminAccount, base['actor_id'])
    kw = sel(base)
    if mutate == 'finance': actor.role = 'finance'
    if mutate == 'inactive': actor.active = False
    if mutate == 'must_change': actor.must_change_password = True
    if mutate == 'pending_reset': actor.pending_credential_reset = True
    if mutate == 'stale_version': actor.session_version += 1
    if mutate == 'foreign_session':
        other = db.scalar(select(AdminAccount).where(AdminAccount.id != actor.id))
        if other is None:
            other = AdminAccount(username='other_l8', password_hash='x', role='assistant_manager')
            db.add(other); db.flush()
        s = db.get(AdminSession, base['session_id']); s.admin_account_id = other.id
    if mutate == 'no_session': kw['session_id'] = 'f' * 64
    if mutate == 'wrong_epoch': kw['expected_epoch'] = 'stale-epoch'
    db.flush()
    with pytest.raises(ValueError):
        review_carry_consumption(db, **kw)
    with pytest.raises(ValueError):
        record_carry_consumption(db, operation_id='c' * 64, reviewed_digest=good['reviewed_digest'], human_confirmed=True, **kw)
