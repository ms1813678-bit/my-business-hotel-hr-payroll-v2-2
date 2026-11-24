"""Approved new leave operator powers, not wired to existing routes.

Host system_admin is the existing HR-manager/administrator identity. This does
not create another login role or change technical, payroll or attendance powers.
Only the explicitly approved new-module actions below are represented. Future
write services must re-check the current authenticated account role and status
inside their transaction. A UI label or this preview alone cannot authorize DML.
"""
from enum import StrEnum


class LeaveAction(StrEnum):
    PREPARE_REQUEST = 'prepare_request'
    APPROVE_REQUEST = 'approve_request'
    ADJUST_BALANCE = 'adjust_balance'
    CLOSE_MONTH = 'close_month'
    VIEW_REPORTS = 'view_reports'
    VIEW_BALANCES = 'view_balances'


_APPROVED = {
    'system_admin': frozenset(LeaveAction),
    'assistant_manager': frozenset({LeaveAction.PREPARE_REQUEST}),
    'finance': frozenset({LeaveAction.VIEW_REPORTS, LeaveAction.VIEW_BALANCES}),
}


def leave_action_allowed(role: str, action: LeaveAction) -> bool:
    """Fail closed for unknown roles/actions, never infer adjacent powers."""
    if not isinstance(role, str) or not isinstance(action, LeaveAction):
        return False
    return action in _APPROVED.get(role, frozenset())


def require_leave_action(role: str, action: LeaveAction) -> None:
    if not leave_action_allowed(role, action):
        raise PermissionError('New leave action is not allowed for this operator.')
