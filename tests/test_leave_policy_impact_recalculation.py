"""Wiring: policy-impact preview carries a bound recalculation preview.

The recalculation field is the exact before/after contract a later authorized
recalculation service re-verifies; it must track the reviewed source and never
change the digested payload or persistence.
"""
from datetime import date
from decimal import Decimal

from app.leave_policy_impact import preview_earned_policy_impact
from app.leave_recalculation_preview import preview_recalculation
from tests.test_leave_policy_impact import setup_impact
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_department_workbook_store import context  # noqa


def test_impact_carries_bound_recalculation_preview(history_context):
    db, e, args = setup_impact(history_context)
    result = preview_earned_policy_impact(db, **args)
    recalculation = result.recalculation
    assert recalculation is not None
    before = {(row.employee_id, row.code): row.before_earned for row in result.rows}
    after = {(row.employee_id, row.code): row.after_earned for row in result.rows}
    expected = preview_recalculation(before, after, source_digest=recalculation.source_digest)
    assert recalculation == expected
    assert len(recalculation.preview_digest) == 64
    # The sick-day policy change moves annual earned only; delta agrees with rows.
    annual = next(c for c in recalculation.changes if c.code == 'A')
    assert annual.delta == next(r for r in result.rows if r.code == 'A').earned_delta
    assert annual.delta < 0


def test_recalculation_tracks_source_and_stays_out_of_digest(history_context):
    db, e, args = setup_impact(history_context)
    first = preview_earned_policy_impact(db, **args)
    from sqlalchemy import select
    from app.models import LeaveRegisterEntry
    day = db.scalar(select(LeaveRegisterEntry).where(
        LeaveRegisterEntry.employee_id == e.id, LeaveRegisterEntry.work_date == date(2026, 2, 26)))
    day.code = ''
    db.flush()
    second = preview_earned_policy_impact(db, **args)
    assert second.source_digest != first.source_digest
    assert second.recalculation.source_digest != first.recalculation.source_digest
    assert second.recalculation.preview_digest != first.recalculation.preview_digest
    assert not second.recalculation.changes  # identical register -> no earned change
