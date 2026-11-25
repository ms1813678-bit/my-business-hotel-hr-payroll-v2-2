"""Functional review of the payroll-run card layout (table -> cards move).

Covers the rendered structure and the actual submit flows the cards carry:
toggle targets, search data, edit/save, conflict review, payments history,
correction, record-payment and the payslip PDF link, draft and closed states.
"""
import re
from html.parser import HTMLParser

from sqlalchemy import select

from app.models import PayrollItem, PayrollPayment
from test_payroll_guards import (  # noqa: F401  (fixture + helpers)
    add_leave,
    approve_run,
    close_run,
    create_run,
    csrf,
    guard_client,
    login,
    pay,
    set_absence,
)

EDIT_FIELDS = {
    "attendance_bonus", "performance_bonus", "occupancy_bonus", "overtime_hours",
    "other_bonus", "absence_days", "lateness_deduction", "insurance_share",
    "taxes", "other_deductions", "note",
}


def test_draft_run_cards_render_with_working_edit_form(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    page = client.get(f"/payroll/runs/{run_id}").text

    # Cards replaced the table: one card per item, searchable, with toggle.
    assert 'class="payroll-card payroll-summary-row"' in page
    assert "MY-TEST-GRD-0001 Synthetic Guard Employee" in page  # data-search
    assert 'id="employee-search"' in page
    assert 'class="payroll-edit-toggle' in page
    assert "payroll-avatar" in page
    assert '<div class="payroll-edit-row" hidden>' in page
    # Payslip link present on the card.
    assert f'href="/payroll/runs/{run_id}/items/{item_id}/payslip.pdf"' in page
    # The edit form kept every field, inside the card.
    form = re.search(
        r'<form method="post" action="/payroll/items/%d/update"(.*?)</form>' % item_id,
        page, re.S)
    assert form, "edit form missing"
    names = set(re.findall(r'name="([^"]+)"', form.group(1)))
    assert EDIT_FIELDS - {"overtime_hours"} - names == set(), EDIT_FIELDS - names
    assert "overtime_hours" not in names
    assert f'href="/payroll/items/{item_id}/overtime-days"' in page
    assert "csrf_token" in names


def test_card_edit_form_saves_and_payslip_pdf_serves(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    page = client.get(f"/payroll/runs/{run_id}")
    response = client.post(
        f"/payroll/items/{item_id}/update",
        data={
            "csrf_token": csrf(page),
            "attendance_bonus": "0", "performance_bonus": "150.50", "occupancy_bonus": "0",
            "overtime_hours": "0", "other_bonus": "0", "absence_days": "0",
            "lateness_deduction": "0", "insurance_share": "0", "taxes": "0",
            "other_deductions": "0", "note": "ملاحظة تجريبية",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:200]
    with client.test_session_factory() as db:
        item = db.get(PayrollItem, item_id)
        assert float(item.performance_bonus) == 150.50
        assert item.note == "ملاحظة تجريبية"
    pdf = client.get(f"/payroll/runs/{run_id}/items/{item_id}/payslip.pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"].startswith("application/pdf")


def test_conflict_review_panel_stays_open_and_submits(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    set_absence(client, item_id, 1)
    add_leave(client, "ANNUAL", "confirmed")
    page = client.get(f"/payroll/runs/{run_id}")
    # Unresolved conflict: the card's panel is expanded, not hidden.
    assert '<div class="payroll-edit-row">' in page.text
    assert "تأكيد مراجعة التعارض" in page.text
    response = client.post(
        f"/payroll/items/{item_id}/absence-leave-review",
        data={"csrf_token": csrf(page)},
        follow_redirects=False,
    )
    assert response.status_code == 303
    page = client.get(f"/payroll/runs/{run_id}").text
    assert "تمت مراجعة التعارض" in page


def test_closed_run_card_payments_history_correction_and_new_payment(guard_client):
    client = guard_client
    login(client)
    run_id, item_id = create_run(client)
    client.test_run_id = run_id
    approve_run(client, run_id)
    close_run(client, run_id)
    pay(client, item_id, 1000)
    page = client.get(f"/payroll/runs/{run_id}")
    text = page.text
    # Closed state: الدفعات toggle + history table + correction + record forms.
    assert "الدفعات" in text
    assert "تصحيح تسجيل دفعة" in text
    assert "تسجيل دفعة" in text
    with client.test_session_factory() as db:
        payment = db.scalar(select(PayrollPayment).where(PayrollPayment.payroll_item_id == item_id))
    assert f'action="/payroll/payments/{payment.id}/correct"' in text
    assert f'action="/payroll/items/{item_id}/payments"' in text
    # Correction flow through the moved form.
    response = client.post(
        f"/payroll/payments/{payment.id}/correct",
        data={"csrf_token": csrf(page), "corrected_amount": "900", "reason": "تصحيح تجريبي"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:200]
    with client.test_session_factory() as db:
        corrected = db.get(PayrollPayment, payment.id)
        assert corrected.superseded_by_payment_id is not None
    # Record-payment form still works from the card.
    page = client.get(f"/payroll/runs/{run_id}")
    response = client.post(
        f"/payroll/items/{item_id}/payments",
        data={"csrf_token": csrf(page), "amount": "250", "paid_date": "2026-11-06",
              "method": "cash", "reference": "RCP-1"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:200]
    with client.test_session_factory() as db:
        refs = [p.reference for p in db.scalars(select(PayrollPayment)).all()]
        assert "RCP-1" in refs


def test_new_product_run_requires_daily_overtime(guard_client):
    from app.models import PayrollRun
    client=guard_client;login(client)
    run_id,item_id=create_run(client)
    with client.test_session_factory() as db:
        assert db.get(PayrollRun,run_id).overtime_daily_required is True
