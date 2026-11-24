import pytest

from app.leave_identity_preview import EmployeeIdentity, preview_identity_match

IDENTITIES = (EmployeeIdentity(1, '001', 'Synthetic One'),
              EmployeeIdentity(2, '002', 'Synthetic Duplicate'),
              EmployeeIdentity(3, '003', 'Synthetic Duplicate'))


@pytest.mark.parametrize(('code', 'name', 'expected'), [
    ('001', 'Synthetic One', (1, None)),
    (' 001 ', None, (1, None)),
    (None, 'Synthetic One', (1, None)),
    (None, 'Synthetic Duplicate', (None, 'duplicate_employee_name')),
    ('009', 'Synthetic One', (None, 'employee_code_not_found')),
    ('001', 'Synthetic Duplicate', (None, 'employee_code_name_conflict')),
    (None, None, (None, 'missing_employee_identity')),
    ('1', None, (None, 'employee_code_not_found')),
    (None, 'synthetic one', (None, 'employee_name_not_found')),
])
def test_exact_evidence_never_fuzzy_links_or_guesses(code, name, expected):
    result = preview_identity_match(source_code=code, source_name=name, identities=IDENTITIES)
    assert (result.employee_id, result.issue) == expected


def test_duplicate_host_codes_never_choose_first():
    duplicate = IDENTITIES + (EmployeeIdentity(4, '001', 'Other Synthetic'),)
    result = preview_identity_match(source_code='001', source_name=None, identities=duplicate)
    assert result.employee_id is None
    assert result.issue == 'duplicate_employee_code'


def test_numeric_excel_cell_needs_explicit_conversion_with_quality_report():
    with pytest.raises(TypeError):
        preview_identity_match(source_code=1, source_name=None, identities=IDENTITIES)


def test_duplicate_host_id_is_invalid_evidence_not_duplicate_import():
    with pytest.raises(ValueError):
        preview_identity_match(source_code='001', source_name=None,
                               identities=IDENTITIES + (IDENTITIES[0],))
