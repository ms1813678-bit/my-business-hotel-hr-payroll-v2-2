from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app.leave_close_preview import BackupEvidence, validate_close_prerequisites


@pytest.fixture
def verified():
    return BackupEvidence(7, 'success', datetime(2026, 1, 31, tzinfo=UTC),
                          'synthetic-external-drive/archive.hrb', 'a' * 64)


def test_explicit_verified_backup_policy_serialization(verified):
    value = validate_close_prerequisites(verified, {'annual_deduct_sick': False, 'cash_basis': 'prorated'})
    assert 'annual_deduct_sick' in value
    assert 'prorated' in value
    assert verified.run_id == 7


@pytest.mark.parametrize('changes', [{'status': 'failed'}, {'verified_at': None},
    {'archive_path': None}, {'sha256': None}, {'sha256': 'wrong'}, {'run_id': 0},
    {'verified_at': datetime(2026, 1, 31)}])
def test_failed_or_unverified_backup_cannot_satisfy_close(verified, changes):
    with pytest.raises(ValueError):
        validate_close_prerequisites(replace(verified, **changes), {'cash_basis': 'prorated'})


def test_policy_defaults_never_inferred(verified):
    with pytest.raises(ValueError):
        validate_close_prerequisites(verified, {})
    with pytest.raises(ValueError):
        validate_close_prerequisites(verified, {'bad': float('nan')})
