import pytest

from app.leave_operator_permissions import LeaveAction, leave_action_allowed, require_leave_action


@pytest.mark.parametrize('action', list(LeaveAction))
def test_hr_manager_can_perform_explicitly_approved_actions(action):
    assert leave_action_allowed('system_admin', action)


@pytest.mark.parametrize('role', ['assistant_manager', 'finance', 'employee',
                                  'department_manager', 'hr_manager', '', None])
@pytest.mark.parametrize('action', [LeaveAction.APPROVE_REQUEST, LeaveAction.ADJUST_BALANCE,
                                    LeaveAction.CLOSE_MONTH])
def test_final_approval_adjustment_and_closure_remain_hr_only(role, action):
    with pytest.raises(PermissionError):
        require_leave_action(role, action)


def test_finance_read_only_and_assistant_preparation_are_distinct():
    assert leave_action_allowed('assistant_manager', LeaveAction.PREPARE_REQUEST)
    assert not leave_action_allowed('finance', LeaveAction.PREPARE_REQUEST)
    assert leave_action_allowed('finance', LeaveAction.VIEW_REPORTS)
    assert leave_action_allowed('finance', LeaveAction.VIEW_BALANCES)


def test_unknown_action_or_raw_action_string_never_infers_permission():
    assert not leave_action_allowed('system_admin', 'close_month')
    assert not leave_action_allowed('system_admin', 'reopen_month')
    assert not leave_action_allowed('system_admin', None)
