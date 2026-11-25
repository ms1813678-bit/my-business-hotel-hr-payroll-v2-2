import pytest
from datetime import date
from app import weekly_summary as weekly
from app.national_id import is_valid_egyptian_national_id
from app.models import AuditLog
from app.leave_month_earning import review_month_earning
from tests.test_leave_month_earning import monthly  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_department_workbook_store import context  # noqa


def test_weekly_summary_counts_confirmed_leave_as_approved_label():
    # leave_records.status is constrained to draft/confirmed/cancelled; 'approved' never occurs.
    report = {"start": "2026-11-16", "end": "2026-11-22", "attendance_review": 0, "attendance_approved": 0,
              "leaves": {"draft": 1, "confirmed": 2, "cancelled": 3}, "advances": {}, "staff_actions": 0,
              "hires": 0, "latest_payroll": None}
    assert "2 approved" in weekly.format_weekly_summary(report, "en")
    assert "2 معتمدة" in weekly.format_weekly_summary(report, "ar")
    assert "2 条已批准" in weekly.format_weekly_summary(report, "zh")


@pytest.mark.parametrize('value', ['29' + '²' * 2 + '01010101234', '2\u0669' + '0' * 12, '2\u0662' + '01010101234' + '0'])
def test_national_id_non_ascii_digits_refuse_without_exception(value):
    assert is_valid_egyptian_national_id(value[:14].ljust(14, '0')) is False


@pytest.mark.parametrize('raw', ['not json', '[]', '"x"', '{"employee_id": null}', '{"employee_id": %d}',
    '{"employee_id": %d, "ledger": null}', '{"employee_id": %d, "ledger": [{}]}'])
def test_malformed_release_audit_is_a_controlled_refusal_never_ignored(monthly, raw):
    db, e, kinds, args = monthly
    if '%d' in raw: raw = raw % e.id
    db.add(AuditLog(actor='synthetic', action='leave_annual_released', entity_type='leave_annual_release',
                    entity_id='x', new_value=raw))
    db.flush()
    with pytest.raises(ValueError, match='Malformed annual release audit evidence'):
        review_month_earning(db, **args(date(2026, 2, 1), date(2026, 2, 28)))


def test_other_employee_wellformed_release_audit_does_not_block(monthly):
    db, e, kinds, args = monthly
    db.add(AuditLog(actor='synthetic', action='leave_annual_released', entity_type='leave_annual_release',
                    entity_id='x', new_value='{"employee_id": %d, "ledger": []}' % (e.id + 999)))
    db.flush()
    assert review_month_earning(db, **args(date(2026, 2, 1), date(2026, 2, 28)))['reviewed_digest']
