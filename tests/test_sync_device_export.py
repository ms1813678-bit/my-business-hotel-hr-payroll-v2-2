import pytest
from app import sync_device_export as x
from app import sync_visibility as v

ASSISTANT_ONLY = {"branch_name"}
SALARY_LIKE = {"basic_salary", "national_id", "passport_number", "phone", "address", "birth_date",
               "fingerprint_id", "prior_insurance_years", "probation_start_date", "probation_end_date", "daily_hours", "created_at"}


def row(entity, field, value="v", seq=1, uid="u1"):
    return {"entity": entity, "uid": uid, "field": field, "value": value, "main_seq": seq,
            "origin_device_id": "d1", "origin_counter": seq, "restore_epoch": 0}


def all_rows():
    rows, n = [], 0
    for ent, per in v.READABLE.items():
        for f in sorted(set().union(*per.values())):
            n += 1
            rows.append(row(ent, f, seq=n))
    rows.append(row("employees", "basic_salary", "999", n + 1))
    rows.append(row("employees", "branch_id", "7", n + 2))
    rows.append(row("employees", "photo_path", "C:/x", n + 3))
    rows.append(row("payroll", "net", "1", n + 4))
    return rows


def test_assistant_never_gets_salary_or_private_fields():
    out = x.filter_rows(all_rows(), v.ROLE_ASSISTANT)
    fields = {r["field"] for r in out if r["entity"] == "employees"}
    assert not (fields & SALARY_LIKE)
    assert fields == set(v.READABLE["employees"][v.ROLE_ASSISTANT]) - {"branch_name"}


def test_finance_gets_only_its_columns_and_no_assistant_derived_data():
    out = x.filter_rows(all_rows(), v.ROLE_FINANCE, branch_name_for=lambda uid: "Maadi")
    for r in out:
        assert r["field"] in v.visible_columns(r["entity"], v.ROLE_FINANCE)
    assert not any(r["field"] in ("branch_name", "branch_id", "photo_path") for r in out)
    assert not any(r["entity"] == "payroll" for r in out)


def test_branch_name_derived_for_assistant_only_and_id_never_sent():
    out = x.filter_rows([row("employees", "branch_id", "7", 5)], v.ROLE_ASSISTANT, lambda uid: "Maadi")
    assert [(r["field"], r["value"], r["main_seq"]) for r in out] == [("branch_name", "Maadi", 5)]
    assert x.filter_rows([row("employees", "branch_id", "7")], v.ROLE_ASSISTANT) == []
    assert x.filter_rows([row("employees", "branch_id", "7")], v.ROLE_FINANCE, lambda u: "Maadi") == []


def test_unknown_role_entity_field_yield_nothing():
    assert x.filter_rows(all_rows(), "admin") == []
    assert x.filter_rows(all_rows(), None) == []
    assert x.filter_rows([row("nope", "name"), row("employees", "id"), row("employees", "sync_uid")], v.ROLE_ADMIN) == []


@pytest.mark.parametrize("role,entity,field,ok", [
    (v.ROLE_ASSISTANT, "employees", "basic_salary", False),
    (v.ROLE_ASSISTANT, "employees", "full_name", True),
    (v.ROLE_ASSISTANT, "employees", "branch_name", False),
    (v.ROLE_ASSISTANT, "employees", "branch_id", False),
    (v.ROLE_ASSISTANT, "employees", "department_uid", True),
    (v.ROLE_FINANCE, "employees", "basic_salary", True),
    (v.ROLE_FINANCE, "employees", "branch_name", False),
    ("bogus", "employees", "full_name", False),
    (v.ROLE_FINANCE, "employees", "id", False),
    (v.ROLE_FINANCE, 5, "name", False),
])
def test_validate(role, entity, field, ok):
    assert x.validate(role, entity, field) is ok


def test_export_for_device_uses_fetch_and_preserves_order_and_attrs():
    class R:
        def __init__(self, **k): self.__dict__.update(k)
    rows = [R(**row("employees", "full_name", "a", 1)), R(**row("employees", "basic_salary", "9", 2)), R(**row("departments", "name", "d", 3))]
    seen = {}
    def fetch(db, s): seen["a"] = (db, s); return rows
    out = x.export_for_device("DB", v.ROLE_ASSISTANT, 0, fetch)
    assert seen["a"] == ("DB", 0) and [r["main_seq"] for r in out] == [1, 3]
