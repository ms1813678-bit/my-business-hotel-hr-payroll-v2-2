"""Unwired authenticated preparation-only register edit service.

No route/UI, request approval, balance posting, attendance or payroll change.
The explicit source period is not a chosen monthly cycle. Closed source windows
and legacy linked leave always refuse, not reopen or reconcile by assumption.
"""
import json
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.leave_register_review import RegisterEdit, prepare_register_edit
from app.models import AdminAccount, AdminSession, AuditLog, LeaveRegisterEntry, PayrollRun, utcnow


def save_reviewed_register_preparation(engine, *, edit: RegisterEdit,
                                       actor_id: int, session_id: str,
                                       session_version: int, reviewed_digest: str,
                                       _review_check=None, _review_consume=None) -> tuple[int, int]:
    """Persist factual preparation and audit atomically, never an approval.

    Caller must derive identity/session/version from an authenticated request and
    enforce CSRF plus human review before invocation. A source/revision digest
    does not substitute for authentication or authorize a confirmed leave.
    HR and assistant may record preparation; finance cannot. Any changed source
    requires a fresh review, including retry after a committed edit.
    """
    # Validate proposal before opening or locking the database.
    if not isinstance(edit, RegisterEdit):
        raise TypeError('An explicit register preparation is required.')
    if (type(actor_id) is not int or actor_id < 1 or type(session_version) is not int
            or not isinstance(session_id, str) or not session_id
            or not isinstance(reviewed_digest, str) or len(reviewed_digest) != 64
            or any(c not in '0123456789abcdef' for c in reviewed_digest)):
        raise ValueError('Current authenticated session and reviewed digest required.')
    if engine.dialect.name != 'postgresql':
        raise RuntimeError('Register preparation requires migrated PostgreSQL locking semantics.')
    with Session(engine) as db, db.begin():
        db.execute(text("SET LOCAL lock_timeout = '3s'"))
        db.execute(text('LOCK TABLE leave_register_entries, leave_month_close, leave_records, '
                        'employee_service_periods, employees IN SHARE ROW EXCLUSIVE MODE'))
        actor = db.scalar(select(AdminAccount).where(AdminAccount.id == actor_id).with_for_update())
        login = db.scalar(select(AdminSession).where(AdminSession.id == session_id).with_for_update())
        if (actor is None or login is None or login.admin_account_id != actor_id
                or not actor.active or actor.role not in {'system_admin', 'assistant_manager'}
                or actor.must_change_password or actor.pending_credential_reset
                or actor.session_version != session_version):
            raise PermissionError('Current authorized register preparer session is required.')
        if _review_check is not None:
            _review_check(db)
        # Approved/closed payroll protects its month's day evidence (parity with
        # leave_return_register). Deliberately a ROW lock on the overlapping runs,
        # not a payroll_runs TABLE lock. Preparation has no common consume gate;
        # adding a table lock here would introduce an ordering dependency with
        # approval's existing leave_month_close lock. The row lock serializes
        # with approve/close (which lock the run row FOR UPDATE after lock_scope).
        # Check status after waiting: approval is seen here or waits and then
        # re-reads the register. This does not authorize new live entry points.
        if isinstance(edit.work_date, date):
            from calendar import monthrange
            first = edit.work_date.replace(day=1)
            last = date(first.year, first.month, monthrange(first.year, first.month)[1])
            runs = db.scalars(select(PayrollRun).where(
                PayrollRun.period_start <= last, PayrollRun.period_end >= first
            ).order_by(PayrollRun.id).with_for_update(read=True).execution_options(populate_existing=True)).all()
            if any(run.status in ('approved', 'closed') for run in runs):
                raise ValueError('Approved/closed payroll blocks register preparation.')
        current = prepare_register_edit(db, edit)
        if current.issues:
            raise ValueError('Register preparation blocked: ' + ','.join(current.issues))
        if current.source_digest != reviewed_digest:
            # No silent retry by value alone: require review after any source/revision change.
            raise ValueError('Reviewed register evidence is stale; review again.')
        if current.unchanged:
            if _review_consume is not None:
                _review_consume(db)
            return current.previous_id, current.previous_revision
        entry = db.get(LeaveRegisterEntry, current.previous_id) if current.previous_id else None
        old = None if entry is None else json.dumps({
            'code': entry.code, 'certificate_attached': entry.certificate_attached,
            'notes': entry.notes, 'source': entry.source, 'revision': entry.revision}, ensure_ascii=True)
        if entry is None:
            entry = LeaveRegisterEntry(employee_id=edit.employee_id, work_date=edit.work_date,
                code=edit.code, certificate_attached=edit.certificate_attached,
                notes=edit.notes, source=edit.source, revision=1, recorded_by=actor.username)
            db.add(entry)
        else:
            entry.code = edit.code
            entry.certificate_attached = edit.certificate_attached
            entry.notes = edit.notes
            entry.source = edit.source
            entry.revision += 1
            entry.recorded_by = actor.username
            entry.recorded_at = utcnow()
        db.flush()
        db.add(AuditLog(actor=actor.username, action='leave_register_prepared',
            entity_type='leave_register_entry', entity_id=str(entry.id), old_value=old,
            new_value=json.dumps({'code': entry.code,
                'certificate_attached': entry.certificate_attached, 'notes': entry.notes,
                'source': entry.source, 'revision': entry.revision,
                'reviewed_digest': reviewed_digest,
                'period_start': edit.period_start.isoformat(),
                'period_end': edit.period_end.isoformat()}, ensure_ascii=True),
            reason='Explicit day evidence preparation, not leave approval'))
        if _review_consume is not None:
            _review_consume(db)
        db.flush()
        return entry.id, entry.revision
