"""Group 4: a partial POST to /payroll/items/{id}/update must keep stored values for absent fields.
Uses the repo's SQLite-backed payroll_client route fixture (not PG). Base 0bc929d + patch."""
from decimal import Decimal
from sqlalchemy import select
from app.models import PayrollItem
from tests.test_payroll_routes import payroll_client, login, csrf, create_test_payroll_run  # noqa: F401

FIELDS = dict(attendance_bonus="11.00", performance_bonus="22.00", occupancy_bonus="33.00", other_bonus="44.00",
              absence_days="1.00", lateness_deduction="55.00", insurance_share="66.00", taxes="77.00",
              other_deductions="88.00")


def _prepare(client):
    login(client)
    run_id = create_test_payroll_run(client)
    with client.test_session_factory.begin() as db:
        from app.models import PayrollRun
        db.get(PayrollRun, run_id).overtime_daily_required = False
        item = db.scalar(select(PayrollItem).where(PayrollItem.payroll_run_id == run_id))
        for k, v in FIELDS.items():
            setattr(item, k, Decimal(v))
        item.note = "keep me"
        item_id = item.id
    return run_id, item_id


def test_partial_post_keeps_stored_values_for_absent_fields(payroll_client):
    client = payroll_client
    run_id, item_id = _prepare(client)
    r = client.post(f"/payroll/items/{item_id}/update",
                    data={"csrf_token": csrf(client.get(f"/payroll/runs/{run_id}")), "taxes": "99.00"},
                    follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"]
    with client.test_session_factory() as db:
        item = db.get(PayrollItem, item_id)
        assert item.taxes == Decimal("99.00")
        for k, v in FIELDS.items():
            if k != "taxes":
                assert getattr(item, k) == Decimal(v), k
        assert item.note == "keep me"


def test_full_form_post_still_sets_every_field_and_can_clear_note(payroll_client):
    client = payroll_client
    run_id, item_id = _prepare(client)
    data = {k: "1.00" for k in FIELDS} | {"note": "", "csrf_token": csrf(client.get(f"/payroll/runs/{run_id}"))}
    r = client.post(f"/payroll/items/{item_id}/update", data=data, follow_redirects=False)
    assert r.status_code == 303 and "error" not in r.headers["location"]
    with client.test_session_factory() as db:
        item = db.get(PayrollItem, item_id)
        assert all(getattr(item, k) == Decimal("1.00") for k in FIELDS) and item.note is None
