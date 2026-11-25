"""Real additive migration rollback and rational evidence retention."""
from decimal import Decimal
import pytest
from sqlalchemy import text
from tests.test_leave_precision_migration import precision_schema,migrate,seed_legacy  # noqa


def test_exact_amount_empty_down_up_and_legacy_bytes_preserved(precision_schema):
    with precision_schema.begin() as c:
        migrate(c,'0069_leave_register_consume')
        seed_legacy(c,Decimal('1.23456789012345678901234567890123456789'))
        before=c.scalar(text('select to_jsonb(t) from leave_balance_transactions t'))
        migrate(c,'head')
        after=c.scalar(text('select to_jsonb(t)-\'exact_numerator\'-\'exact_denominator\' from leave_balance_transactions t'))
        assert after==before
        migrate(c,'0069_leave_register_consume',down=True)
        assert c.scalar(text('select to_jsonb(t) from leave_balance_transactions t'))==before
        migrate(c,'head')


def test_rational_amount_downgrade_refuses_before_ddl(precision_schema):
    with precision_schema.begin() as c:
        migrate(c,'head');seed_legacy(c,Decimal(1))
        row=c.execute(text('select employee_id,leave_type_id from leave_balance_transactions limit 1')).one()
        c.execute(text("insert into leave_balance_transactions(employee_id,leave_type_id,transaction_type,days_delta,exact_numerator,exact_denominator,reason,created_by,created_at) values (:e,:t,'adjustment',NULL,1,7,'Synthetic','synthetic',now())"),{'e':row.employee_id,'t':row.leave_type_id})
        with pytest.raises(RuntimeError,match='rational evidence'):
            migrate(c,'0069_leave_register_consume',down=True)
        assert c.scalar(text("select count(*) from information_schema.columns where table_schema=current_schema() and table_name='leave_balance_transactions' and column_name='exact_numerator'"))==1
        assert c.scalar(text('select count(*) from leave_balance_transactions where exact_denominator=7'))==1
