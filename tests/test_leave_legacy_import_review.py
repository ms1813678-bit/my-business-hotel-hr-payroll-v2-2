"""Wiring: legacy evidence bundle -> exact identity match + host day codes.

Covers the composition the later reviewed leave workbook import will call:
legacy 'P' normalizes through the explicit-present path (owner-settled layout),
blanks stay unknown, and identity matching never fuzzy-links.
"""
from datetime import date

import pytest

from app.legacy_department_evidence import parse_legacy_department, prepare_import_review
from app.leave_identity_preview import EmployeeIdentity
from tests.test_legacy_department_evidence import sample


def review():
    evidence = parse_legacy_department(sample(), source_identity='synthetic:message', year=2026)
    identities = (EmployeeIdentity(7, '1', 'Synthetic employee'),
                  EmployeeIdentity(8, '2', 'Other synthetic'))
    return prepare_import_review(evidence, identities=identities)


def test_identity_rows_match_exactly_and_never_fuzzy():
    result = review()
    assert result.identities[0]['employee_id'] == 7
    assert result.identities[0]['issue'] is None
    # A name that only differs by case never links.
    evidence = parse_legacy_department(sample(), source_identity='synthetic:message', year=2026)
    lowered = prepare_import_review(
        evidence, identities=(EmployeeIdentity(7, '9', 'synthetic Employee'),))
    assert lowered.identities[0]['employee_id'] is None
    assert lowered.identities[0]['issue'] in {'employee_code_not_found', 'employee_name_not_found'}
    assert lowered.unresolved_employees


def test_present_mark_maps_to_host_present_blank_stays_unknown():
    result = review()
    by_cell = {day['cell']: day for day in result.days if day['sheet'] == 'Jan' and day['cell'][1:] == '3'}
    # Jan row 3: 'w' (weekly), None (blank), 'u' (unpaid) for days 1-3.
    assert by_cell['C3']['host_code'] == 'W' and by_cell['C3']['issue'] is None
    assert by_cell['D3']['host_code'] is None  # blank is never presence or absence
    assert by_cell['E3']['host_code'] == 'U'
    present_evidence = parse_legacy_department(sample(), source_identity='s', year=2026)
    # Rewrite one day cell to the owner's settled present mark.
    days = [dict(d, code='P') if d['cell'] == 'C3' and d['sheet'] == 'Jan' else d
            for d in present_evidence.days]
    patched = type(present_evidence)(present_evidence.source_identity, present_evidence.source_sha256,
        present_evidence.original_bytes, present_evidence.employees, tuple(days), present_evidence.issues)
    mapped = prepare_import_review(patched, identities=(EmployeeIdentity(7, '1', 'Synthetic employee'),))
    cell = next(d for d in mapped.days if d['sheet'] == 'Jan' and d['cell'] == 'C3')
    assert cell['host_code'] == '' and cell['issue'] is None  # host present mark
    # Formula cells stay non-evidence through the composition.
    formulas = [dict(d, code=None) for d in patched.days]
    formula_bundle = type(present_evidence)(present_evidence.source_identity, present_evidence.source_sha256,
        present_evidence.original_bytes, present_evidence.employees, tuple(formulas), present_evidence.issues)
    unmapped = prepare_import_review(formula_bundle, identities=(EmployeeIdentity(7, '1', 'Synthetic employee'),))
    assert all(d['host_code'] is None for d in unmapped.days)
    assert unmapped.unresolved_days


def test_non_bundle_input_refuses():
    with pytest.raises(TypeError):
        prepare_import_review(object(), identities=())
