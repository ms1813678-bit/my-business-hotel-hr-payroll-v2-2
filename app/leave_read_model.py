"""Day-only read helpers for foundation testing, not wired to payroll/routes."""
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.leave_day_calculations import employed_dates
from app.models import Employee, EmployeeServicePeriod, LeaveMonthNote, LeaveRegisterEntry


@dataclass(frozen=True)
class RegisterEvidenceSummary:
    service_evidence_missing: bool
    employed_days: int
    unknown_days: int
    present_days: int
    codes: dict[str, int]
    notes: tuple[str, ...]
    conflicting_dates: tuple[date, ...]


def register_evidence_summary(db: Session, employee_id: int,
                              start: date, end: date) -> RegisterEvidenceSummary:
    """Read facts only; no inferred presence/U and no balance/accrual posting.

    Existing authoritative service periods are required. Missing periods return
    no employed dates, never silently fabricate employment from active status.
    Entries outside service dates are surfaced as conflicts, not counted.
    """
    # Reads must not flush unrelated pending payroll/register changes.
    with db.no_autoflush:
        if db.get(Employee, employee_id) is None:
            raise ValueError("Employee does not exist; no empty report inferred.")
        periods = db.scalars(select(EmployeeServicePeriod).where(
            EmployeeServicePeriod.employee_id == employee_id)).all()
        employed = employed_dates(start, end, [(p.start_date, p.end_date) for p in periods])
        entries = db.scalars(select(LeaveRegisterEntry).where(
            LeaveRegisterEntry.employee_id == employee_id,
            LeaveRegisterEntry.work_date >= start,
            LeaveRegisterEntry.work_date <= end).order_by(LeaveRegisterEntry.work_date)).all()
        codes = dict.fromkeys(('U', 'S', 'A', 'W', 'H'), 0)
        present = 0
        known = set()
        conflicts = []
        notes = []
        for entry in entries:
            if entry.notes:
                notes.append(entry.notes)
            if entry.work_date not in employed:
                if entry.code not in (None, '—'):
                    conflicts.append(entry.work_date)
                continue
            if entry.code in codes:
                codes[entry.code] += 1
                known.add(entry.work_date)
            elif entry.code == '':
                present += 1
                known.add(entry.work_date)
            elif entry.code == '—':
                conflicts.append(entry.work_date)
        month_notes = db.scalars(select(LeaveMonthNote).where(
            LeaveMonthNote.employee_id == employee_id,
            (LeaveMonthNote.year * 12 + LeaveMonthNote.month) >= start.year * 12 + start.month,
            (LeaveMonthNote.year * 12 + LeaveMonthNote.month) <= end.year * 12 + end.month,
        ).order_by(LeaveMonthNote.year, LeaveMonthNote.month)).all()
        notes.extend(note.notes for note in month_notes)
        return RegisterEvidenceSummary(not periods, len(employed), len(employed - known), present,
                                       codes, tuple(notes), tuple(conflicts))
