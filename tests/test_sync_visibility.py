"""Visibility map for the four sync_uid entities against the real models and role source. No PostgreSQL needed."""
import pytest

from app import sync_visibility as sv
from app.dependencies import ROLE_ASSISTANT, ROLE_FINANCE, ROLE_SYSTEM_ADMIN
from app.sync_uid import ENTITIES, OUT_OF_SCOPE_REFS

PRIVATE_FOR_ASSISTANT = {
    "national_id", "passport_number", "basic_salary", "phone", "address", "birth_date", "fingerprint_id",
    "prior_insurance_years", "probation_start_date", "probation_end_date", "daily_hours", "position",
}


def test_role_names_match_the_application_roles():
    assert sv.ROLES == (ROLE_SYSTEM_ADMIN, ROLE_FINANCE, ROLE_ASSISTANT)


def test_every_entity_is_covered_and_every_real_column_is_classified():
    assert set(sv.READABLE) == set(ENTITIES)
    for entity, model in ENTITIES.items():
        columns = {c.name for c in model.__table__.c} - {"id", "sync_uid"}
        readable = set().union(*(sv.READABLE[entity][role] for role in sv.ROLES))
        readable -= {column for (name, column) in sv.DERIVED_COLUMNS if name == entity}
        denied = {column for (name, column) in sv.DENIED_ALL if name == entity}
        assert columns == readable | denied, (entity, sorted(columns ^ (readable | denied)))
        assert not readable & denied


def test_readable_names_are_real_non_identity_columns_and_not_out_of_scope_refs():
    for entity, model in ENTITIES.items():
        real = {c.name for c in model.__table__.c}
        for role in sv.ROLES:
            cols = sv.READABLE[entity][role] - {c for (n, c) in sv.DERIVED_COLUMNS if n == entity}
            assert cols <= real and not cols & {"id", "sync_uid"}
            assert not cols & set(OUT_OF_SCOPE_REFS.get(entity, {}))


def test_denied_entries_name_real_columns_and_have_reasons():
    for (entity, column), reason in sv.DENIED_ALL.items():
        assert column in {c.name for c in ENTITIES[entity].__table__.c} and reason


def test_assistant_employee_columns_equal_the_report_attendance_safe_fields():
    from app.main import _ATTENDANCE_SAFE_FIELDS
    column_for = {"emp_name": "full_name", "emp_code": "code", "emp_department": "department_id",
                  "emp_nationality": "nationality", "emp_hire_date": "hire_date"}
    derived = {column_for[key] for key in _ATTENDANCE_SAFE_FIELDS if key.startswith("emp_")}
    # Mo's answer (voice note 2026-11-26 19:12:40): the assistant device also gets employee status.
    assert derived | {"status", "branch_name"} == set(sv.visible_columns("employees", ROLE_ASSISTANT))
    assert "emp_id_proof" not in _ATTENDANCE_SAFE_FIELDS


def test_assistant_never_receives_identifiers_salary_or_contact_data():
    assert not set(sv.visible_columns("employees", ROLE_ASSISTANT)) & PRIVATE_FOR_ASSISTANT


def test_admin_and_finance_employee_read_matches_the_route_bundle():
    from app.dependencies import require_employee_read  # exists: admin + finance only
    assert require_employee_read is not None
    assert sv.visible_columns("employees", ROLE_SYSTEM_ADMIN) == sv.visible_columns("employees", ROLE_FINANCE)
    assert "basic_salary" in sv.visible_columns("employees", ROLE_FINANCE)


@pytest.mark.parametrize("entity,role", [("payroll_runs", ROLE_SYSTEM_ADMIN), ("employees", "guest"), (None, ROLE_SYSTEM_ADMIN),
                                         ("employees", None), (3, 4), ("employees", ROLE_SYSTEM_ADMIN.upper())])
def test_unknown_entity_or_role_gets_nothing(entity, role):
    assert sv.visible_columns(entity, role) == frozenset()
    assert sv.project(entity, role, {"full_name": "x"}) == {}


def test_project_drops_id_uid_unknown_and_denied_keys():
    row = {"id": 7, "sync_uid": "u", "full_name": "x", "national_id": "n", "photo_path": "p", "branch_id": 1, "zzz": 1}
    assert sv.project("employees", ROLE_ASSISTANT, row) == {"full_name": "x"}
    assert set(sv.project("employees", ROLE_SYSTEM_ADMIN, row)) == {"full_name", "national_id"}
    assert sv.project("employees", ROLE_SYSTEM_ADMIN, "not a dict") == {}


def test_mutating_a_returned_set_cannot_widen_access():
    assert isinstance(sv.visible_columns("employees", ROLE_ASSISTANT), frozenset)


def test_finance_device_gets_identity_and_contact_columns():
    cols = sv.visible_columns("employees", ROLE_FINANCE)
    assert {"national_id", "passport_number", "phone", "address", "birth_date"} <= cols


def test_assistant_device_gets_employee_status_but_not_pay_or_identity():
    cols = sv.visible_columns("employees", ROLE_ASSISTANT)
    assert "status" in cols
    assert not cols & {"basic_salary", "national_id", "passport_number", "phone", "address", "birth_date"}


def test_assistant_device_gets_leave_reasons_and_actor_names():
    cols = sv.visible_columns("leave_records", ROLE_ASSISTANT)
    assert {"reason", "cancellation_reason", "created_by", "confirmed_by", "cancelled_by"} <= cols


def test_assistant_gets_branch_name_only_never_the_branch_id():
    cols = sv.visible_columns("employees", ROLE_ASSISTANT)
    assert "branch_name" in cols and "branch_id" not in cols
    assert ("employees", "branch_name") in sv.DERIVED_COLUMNS and ("employees", "branch_id") in sv.DENIED_ALL
    assert "branch_name" not in sv.visible_columns("employees", ROLE_FINANCE) | sv.visible_columns("employees", ROLE_SYSTEM_ADMIN)
    out = sv.project("employees", ROLE_ASSISTANT, {"branch_id": 7, "branch_name": "Maadi", "status": "active", "id": 1})
    assert out == {"branch_name": "Maadi", "status": "active"}


def test_derive_branch_name():
    class B:
        name = "Maadi"
    assert sv.derive_branch_name(B()) == "Maadi"
    assert sv.derive_branch_name(None) is None
