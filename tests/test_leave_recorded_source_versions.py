# ruff: noqa: F811
"""Focused version-router boundaries, real recorded proofs, no consumer wiring."""
import copy
import json
import pytest
from app.leave_recorded_source_versions import verify_versioned_recorded_source
from app.leave_year2_recorded_source import build_year2_recorded_source
from app.leave_month_earning import digest
from tests.test_department_workbook_store import context  # noqa: F401
from tests.test_leave_historical_evidence import history_context  # noqa: F401
from tests.test_leave_completed_year_source import prior_year  # noqa: F401
from tests.test_l8_year2_net_source import full_year, FULL


@pytest.mark.parametrize('prior_year', [{'hire_date': FULL, 'used_annual': 6}], indirect=True)
def test_version_router_binds_caller_year_amount_held_and_identity(prior_year, monkeypatch):
    db, args = full_year(prior_year, monkeypatch)
    built = build_year2_recorded_source(db, **args)
    result = {**built['stored'], 'held_annual': built['stored']['source']['held_annual']}
    def check(value):
        return verify_versioned_recorded_source(db, result=value,
            employee_id=args['employee_id'], leave_type_id=args['leave_type_id'])
    verified = check(result)
    assert verified['leave_type_code'] == 'weekly_rest'
    assert verified['amount'] == result['source']['own_amount']
    for missing in ('source', 'source_digest', 'source_year', 'carried_amount', 'held_annual'):
        bad = copy.deepcopy(result); del bad[missing]
        with pytest.raises(ValueError): check(bad)
    for key, value in [('source_year', 2025), ('source_year', True),
                       ('carried_amount', '999/1'), ('held_annual', '1/1'),
                       ('held_annual', '0/0')]:
        bad = copy.deepcopy(result); bad[key] = value
        with pytest.raises(ValueError): check(bad)
    for key, value in [('version', 'unknown'), ('source_year', True),
                       ('employee_id', True), ('leave_type_id', True)]:
        bad = copy.deepcopy(result); bad['source'][key] = value
        bad['source_digest'] = digest(bad['source'])
        with pytest.raises(ValueError): check(bad)
    # Deleting the version must fail legacy exact keys, not reinterpret the proof.
    bad = copy.deepcopy(result); del bad['source']['version']
    bad['source_digest'] = digest(bad['source'])
    with pytest.raises(ValueError): check(bad)
    assert check(result) == verified


def test_router_legacy_branch_uses_unchanged_exact_verifier(prior_year):
    from app.leave_completed_year_source import review_completed_year_source
    from app.leave_exact_amount import canonical_amount
    db, _employee, _kinds, args = prior_year
    source = review_completed_year_source(db, **args)
    result = dict(source=source['source'], source_digest=source['source_digest'],
        carried_amount=canonical_amount(source['amount']),
        held_annual=canonical_amount(source['held_annual']), source_year=args['source_year'])
    result = json.loads(json.dumps(result))
    from app.leave_carry_availability import _verify_recorded_source
    assert verify_versioned_recorded_source(db, result=result,
        employee_id=args['employee_id'], leave_type_id=args['leave_type_id']) == _verify_recorded_source(
            db, result=result, employee_id=args['employee_id'], leave_type_id=args['leave_type_id'])
