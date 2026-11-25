"""Actual migrated PostgreSQL/SQLite parity using existing isolated AI fixture."""
from sqlalchemy import select
from tests.test_ai_atomic_corrections import synthetic  # noqa: F401
from app import ai_agent, ai_gateway
from app.models import Employee


def test_text_fingerprint_preview_and_real_write_preserve_zeros(synthetic):
    with synthetic() as db:
        employee = db.scalar(select(Employee))
        identifier = employee.id
    for raw, expected in [('  0042  ', '0042'), ('FP-1001', 'FP-1001')]:
        plan = {'action': 'edit_employee', 'employee_id': identifier,
                'fingerprint_id': raw, 'department_id': None,
                'basic_salary': None, 'reason': 'Synthetic identity correction'}
        preview = ai_gateway.build_confirmation_preview(plan)
        assert preview['new_values'] == {'fingerprint_id': expected}
        with synthetic.begin() as db:
            ai_agent.execute_plan(plan, 'synthetic', db=db)
        with synthetic() as db:
            assert db.get(Employee, identifier).fingerprint_id == expected
