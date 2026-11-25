import pytest

def test_payroll_history_malformed_value_is_controlled_refusal():
    import pytest
    from app.payroll_workflow import _coerce_employee_history_value as c

    for field, bad in (("basic_salary", "abc"), ("daily_hours", "NaN"), ("department_id", "x1")):
        with pytest.raises(ValueError, match="reconcile the employee change history"):
            c(field, bad)
    assert str(c("basic_salary", "5000.50")) == "5000.50" and c("department_id", "7") == 7
