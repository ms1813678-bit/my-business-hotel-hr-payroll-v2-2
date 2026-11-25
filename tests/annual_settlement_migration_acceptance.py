from sqlalchemy import select,text
from app.db import SessionLocal
from app.models import LeaveAnnualSettlement
from tests.annual_settlement_http_acceptance import test_real_annual_http_confirmed_cash_and_carry as exercise_http
from tests.test_department_workbook_store import context  # noqa
from tests.test_leave_historical_evidence import history_context  # noqa
from tests.test_leave_completed_year_source import prior_year  # noqa
import pytest
from datetime import date

@pytest.mark.parametrize('prior_year',[{'hire_date':date(2025,1,1),'used_annual':6}],indirect=True)
def test_populated_annual_migration_evidence_immutable(prior_year,monkeypatch):
    exercise_http(prior_year,monkeypatch)
    with SessionLocal() as db:
        assert db.scalar(select(LeaveAnnualSettlement.id))
        for sql in ('UPDATE leave_annual_settlements SET source_year=2024','DELETE FROM leave_annual_settlements','TRUNCATE leave_annual_settlements CASCADE'):
            with pytest.raises(Exception,match='immutable'):
                with db.begin_nested(): db.execute(text(sql))
