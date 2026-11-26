# ruff: noqa: F811
# Imported pytest fixtures intentionally share test parameter names.
"""Adversarial PostgreSQL checks for later-year source rebind lineage and restore epoch."""
from decimal import Decimal

import pytest

from app.leave_year2_net_source import review_year2_net_source
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_leave_carry_consumption import ready, FULL  # noqa: F401
from tests.test_l8_year2_net_source import full_year
from tests.test_restore_epoch_rebind import confirm, restore, rebind

IDENT = ('actor_id', 'session_id', 'session_version', 'expected_epoch')


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_net_review_across_opening_rebind(prior_year, monkeypatch):
    db, args = full_year(prior_year, monkeypatch)
    employee = prior_year[1]

    baseline = review_year2_net_source(db, **args)
    assert baseline['source_verified'] and not baseline['posting_allowed']

    auth = {k: args[k] for k in IDENT}
    auth2 = restore(db, auth, 'E1-restored')
    same = confirm(db, employee, auth2, {k: Decimal(0) for k in 'AWH'}, start=employee.hire_date)
    rebind(db, args['selection_id'], same.id, auth2)

    # probe 1: superseded selection, current identity - must not resurrect a verified net
    stale = None
    try:
        stale = review_year2_net_source(db, **{**args, **auth2})
    except ValueError as exc:
        print('PROBE1 superseded-selection refusal:', exc)
    assert stale is None, (
        'net review returned source_verified=%s under a SUPERSEDED opening selection '
        '(digest match pre-rebind: %s)' % (stale['source_verified'],
                                           stale['source_digest'] == baseline['source_digest']))

    # probe 2: identical-content baseline (zeros, same start) - net under the new selection
    # verifies with the same amount (baseline genuinely unchanged, only lineage advanced)
    rebound = review_year2_net_source(db, **{**args, **auth2, 'selection_id': same.id})
    assert rebound['source_verified'] and rebound['amount'] == baseline['amount']
    assert rebound['own_rows_amount'] == baseline['own_rows_amount']
    print('PROBE2 identical-baseline rebind verifies, amount:', rebound['amount'])

    # probe 3: a rebind to a DIFFERENT baseline is impossible by kernel invariant
    # ('Current source must equal the archived opening amounts, basis date and employee.')
    auth3 = restore(db, auth2, 'E2-restored')
    changed = confirm(db, employee, auth3,
                      {'A': Decimal('3.5'), 'W': Decimal(1), 'H': Decimal(0)}, start=employee.hire_date)
    with pytest.raises(ValueError, match='Current source must equal the archived opening'):
        rebind(db, same.id, changed.id, auth3)
    print('PROBE3 different-baseline rebind refused at kernel: archived-opening equality holds')

    # probe 4: stale pre-rebind epoch identity
    with pytest.raises(ValueError):
        review_year2_net_source(db, **args)
