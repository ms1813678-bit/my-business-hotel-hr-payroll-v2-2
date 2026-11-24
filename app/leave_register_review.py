"""Unwired register edit preparation, no persistence or balance effects.

Explicit source period is required. No calendar/rolling-cycle default or reopen
power inferred. A later writer must revalidate this evidence under locks and
fresh authenticated scope; this review object does not authorize a mutation.
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Employee,
    EmployeeServicePeriod,
    LeaveMonthClose,
    LeaveRecord,
    LeaveRegisterEntry,
)


@dataclass(frozen=True)
class RegisterEdit:
    employee_id: int
    work_date: date
    code: str | None
    certificate_attached: bool | None
    notes: str | None
    source: str
    period_start: date
    period_end: date


@dataclass(frozen=True)
class RegisterReview:
    source_digest: str
    previous_id: int | None
    previous_revision: int | None
    previous_code: str | None
    proposed_code: str | None
    issues: tuple[str, ...]
    unchanged: bool


def prepare_register_edit(db: Session, edit: RegisterEdit) -> RegisterReview:
    """Read persisted facts without flushing or overwriting dirty ORM fields.

    Unknown stays unknown; empty string is explicit present. Codes arrive
    normalized from an explicitly reviewed source, not guessed from absence.
    Legacy confirmed leave overlap blocks this generic path pending a dedicated
    reconciliation, never causes a duplicate debit or silent record replacement.
    """
    if not isinstance(edit, RegisterEdit):
        raise TypeError('An explicit register edit is required.')
    if type(edit.employee_id) is not int or edit.employee_id < 1:
        raise ValueError('Resolved employee identity required.')
    if any(type(day) is not date for day in (edit.work_date, edit.period_start, edit.period_end)):
        raise ValueError('Explicit work date and source period required.')
    if not edit.period_start <= edit.work_date <= edit.period_end:
        raise ValueError('Work date must be inside the explicit source period.')
    if edit.code not in (None, '', 'U', 'A', 'S', 'H', 'W', '—'):
        raise ValueError('Explicit normalized code required.')
    if edit.certificate_attached is not None and type(edit.certificate_attached) is not bool:
        raise ValueError('Certificate evidence must be boolean or unknown.')
    if edit.notes is not None and (not isinstance(edit.notes, str) or len(edit.notes) > 4000):
        raise ValueError('Notes must be bounded text or unknown.')
    if (not isinstance(edit.source, str) or not edit.source.strip()
            or edit.source != edit.source.strip() or len(edit.source) > 80):
        raise ValueError('Explicit bounded source label required.')
    with db.no_autoflush:
        employee = db.execute(select(Employee.id, Employee.code, Employee.full_name).where(
            Employee.id == edit.employee_id)).first()
        if employee is None:
            raise ValueError('Employee evidence does not exist.')
        periods = db.execute(select(EmployeeServicePeriod.id, EmployeeServicePeriod.start_date,
            EmployeeServicePeriod.end_date).where(EmployeeServicePeriod.employee_id == edit.employee_id
            ).order_by(EmployeeServicePeriod.id)).all()
        existing = db.execute(select(LeaveRegisterEntry.id, LeaveRegisterEntry.revision,
            LeaveRegisterEntry.code, LeaveRegisterEntry.certificate_attached,
            LeaveRegisterEntry.notes, LeaveRegisterEntry.source, LeaveRegisterEntry.leave_record_id,
            LeaveRegisterEntry.recorded_by).where(
            LeaveRegisterEntry.employee_id == edit.employee_id,
            LeaveRegisterEntry.work_date == edit.work_date)).first()
        closures = db.execute(select(LeaveMonthClose.id, LeaveMonthClose.year, LeaveMonthClose.month,
            LeaveMonthClose.policy_snapshot_json).order_by(LeaveMonthClose.id)).all()
        # Cover the whole supplied source window, not only the edited cell's month.
        closed = [list(row) for row in closures if
            edit.period_start.year * 12 + edit.period_start.month <= row.year * 12 + row.month
            <= edit.period_end.year * 12 + edit.period_end.month]
        confirmed = db.execute(select(LeaveRecord.id, LeaveRecord.start_date, LeaveRecord.end_date,
            LeaveRecord.status, LeaveRecord.leave_type_id, LeaveRecord.charged_days).where(
            LeaveRecord.employee_id == edit.employee_id, LeaveRecord.status == 'confirmed',
            LeaveRecord.start_date <= edit.work_date, LeaveRecord.end_date >= edit.work_date
            ).order_by(LeaveRecord.id)).all()
        issues = []
        employed = any(row.start_date <= edit.work_date and (
            row.end_date is None or edit.work_date <= row.end_date) for row in periods)
        if not periods:
            issues.append('employment_evidence_missing')
        elif sum(row.start_date <= edit.work_date and (
                row.end_date is None or edit.work_date <= row.end_date) for row in periods) > 1:
            issues.append('overlapping_employment_evidence')
        elif not employed and edit.code != '—':
            issues.append('day_outside_verified_employment')
        elif employed and edit.code == '—':
            issues.append('outside_service_code_conflicts_with_employment')
        if closed:
            issues.append('source_period_contains_closed_month')
        if confirmed or (existing is not None and existing.leave_record_id is not None):
            issues.append('linked_leave_reconciliation_required')
        unchanged = existing is not None and (
            existing.code, existing.certificate_attached, existing.notes, existing.source) == (
            edit.code, edit.certificate_attached, edit.notes, edit.source)
        payload = {
            'proposal': [edit.employee_id, edit.work_date.isoformat(), edit.code,
                edit.certificate_attached, edit.notes, edit.source,
                edit.period_start.isoformat(), edit.period_end.isoformat()],
            'employee': list(employee), 'service': [list(row) for row in periods],
            'previous': list(existing) if existing else None,
            'closed': closed, 'confirmed': [list(row) for row in confirmed],
        }
        encoded = json.dumps(payload, default=str, ensure_ascii=True,
                             separators=(',', ':'), allow_nan=False).encode()
        return RegisterReview(hashlib.sha256(encoded).hexdigest(),
            existing.id if existing else None, existing.revision if existing else None,
            existing.code if existing else None, edit.code, tuple(issues), unchanged)
