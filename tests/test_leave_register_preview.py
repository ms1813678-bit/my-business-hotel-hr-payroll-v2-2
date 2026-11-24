import pytest

from app.leave_register_preview import normalize_register_code


@pytest.mark.parametrize('value', [None, '', ' ', '\t'])
def test_blank_import_is_unknown_not_absence_or_presence(value):
    result = normalize_register_code(value)
    assert result.code is None
    assert result.issue is None


def test_explicit_present_requires_separate_evidence():
    assert normalize_register_code('', explicit_present=True).code == ''
    assert normalize_register_code('U', explicit_present=True).issue == 'present_evidence_conflicts_with_code'


@pytest.mark.parametrize(('value', 'expected'), [('w', 'W'), (' s ', 'S'), ('a', 'A'),
    ('h', 'H'), ('u', 'U'), ('—', '—')])
def test_codes_are_normalized_without_guessing(value, expected):
    assert normalize_register_code(value).code == expected


@pytest.mark.parametrize('value', ['P', '-', 'absent', 0, True, 1.5])
def test_unknown_code_is_reported_not_guessed(value):
    result = normalize_register_code(value)
    assert result.code is None
    assert result.issue == 'unknown_day_code'


def test_excel_formula_is_not_executed_or_treated_as_status():
    result = normalize_register_code('=COUNTIF(A1:A31,"U")')
    assert result.code is None
    assert result.issue == 'formula_is_not_day_evidence'
