"""Active recorded-source verification stack. Read-only defense in depth, no policy, no posting.

Every versioned/legacy recorded-source verification enters through `guarded()`. The active stack lives in a ContextVar
so unmodified legacy frames in between (_grant, verify_annual_cash_allocation, _verify_plan_rule) are covered without a
signature change. Isolated per thread/async task. The token is always reset in `finally`.

Node = (employee_id, leave_type_id, source_year, level). level: legacy/v1 = 0, v2 = 1. A nested node must have a
strictly LOWER (source_year, level) than the active top: that forbids same-node re-entry and every cycle, and still
allows v2(Y) -> v1(Y) base, v2(Y) -> settlement(Y) -> stored v1(Y), and any earlier-year grant. Siblings are verified
after the earlier frame popped, so they are not "visited". MAX_DEPTH is an explicit resource refusal, not a business limit.
"""
from contextvars import ContextVar

# Documented bounds. (1) Rank compares only (source_year, level), NOT node identity: ANY nested node whose (year, level)
# is not strictly lower than the active top refuses, even for another employee or leave type. Nested verification is
# always of the same employee/type today; identity is enforced by the verifiers, not here. (2) MAX_DEPTH=8 frames is a
# resource bound chosen for this isolated candidate, not an approved business limit; it refuses explicitly.
MAX_DEPTH = 8
_STACK = ContextVar('year2_recorded_source_stack', default=())


def active_stack():
    return _STACK.get()


def guarded(node, fn):
    employee_id, leave_type_id, year, level = node
    if (type(employee_id) is not int or type(leave_type_id) is not int or type(year) is not int
            or type(level) is not int):
        raise ValueError('Recorded source dependency node is not well formed.')
    stack = _STACK.get()
    if len(stack) >= MAX_DEPTH:
        raise ValueError('Proof dependency resource budget exceeded.')
    if stack and (year, level) >= (stack[-1][2], stack[-1][3]):
        raise ValueError('Proof dependency must strictly decrease in rank without re-entry.')
    token = _STACK.set(stack + (node,))
    try:
        return fn()
    finally:
        _STACK.reset(token)
