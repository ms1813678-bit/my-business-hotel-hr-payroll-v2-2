"""Report workbooks must store typed text as text, never as a live formula.

Synthetic values only. Covers the comprehensive HR workbook and the custom
report workbook, in every export language, plus the helper itself.
"""
import re
import zipfile
from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import Workbook, load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import AdvanceRequest, Department, Employee
from app.report_builder import ReportSelection, build_custom_workbook
from app.reports import build_hr_workbook
from app.xlsx_safety import neutralize_formula_cells

DANGEROUS = [
    "=1+1",
    '=HYPERLINK("http://example.invalid","click")',
    "+1+1",
    "-2+3",
    "@SUM(A1)",
    "\t=1+1",
    "=",
]
ORDINARY = ["Ahmed Ali", "أحمد علي", "Sara 1=2"]


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        dept = Department(name="=DEPT()", active=True)
        session.add(dept)
        session.flush()
        for index, name in enumerate(DANGEROUS + ORDINARY, start=1):
            employee = Employee(
                code=f"MY-{index:04d}", full_name=name, department_id=dept.id, position="=POS()",
                hire_date=date(2026, 1, 1), status="active", basic_salary=Decimal("1000"),
                daily_hours=Decimal("8"), phone="@PHONE",
            )
            session.add(employee)
            session.flush()
            session.add(AdvanceRequest(
                employee_id=employee.id, request_date=date(2026, 10, 16), payroll_month=date(2026, 10, 1),
                amount=Decimal("10"), advance_type="regular", salary_basis=Decimal("1000"),
                reason="=REASON()", status="pending", created_by="t",
            ))
        session.commit()
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


def _raw_formula_count(blob: bytes) -> int:
    archive = zipfile.ZipFile(BytesIO(blob))
    return sum(
        len(re.findall(rb"<f[ >]", archive.read(name)))
        for name in archive.namelist() if name.startswith("xl/worksheets/")
    )


def _all_cells(blob: bytes):
    workbook = load_workbook(BytesIO(blob))
    return [(sheet.title, cell) for sheet in workbook for row in sheet.iter_rows() for cell in row if cell.value is not None]


def _custom(db, language):
    selection = ReportSelection(
        report_type="advances", start_date=date(2026, 10, 1), end_date=date(2026, 10, 31),
        period_choice="custom", employee_status="all", fields={"emp_name", "emp_code", "emp_department"},
    )
    return build_custom_workbook(db, selection, language)


@pytest.mark.parametrize("language", ["ar", "en", "zh"])
def test_hr_workbook_has_no_formula_cells_and_keeps_visible_text(db, language):
    blob = build_hr_workbook(db, date(2026, 10, 1), date(2026, 10, 31), language=language)
    assert _raw_formula_count(blob) == 0
    cells = _all_cells(blob)
    assert not [c for _, c in cells if c.data_type == "f"]
    employees = load_workbook(BytesIO(blob))[load_workbook(BytesIO(blob)).sheetnames[0]]
    names = {c.value for row in employees.iter_rows() for c in row if isinstance(c.value, str)}
    assert "=1+1" in names and "=DEPT()" in names and "@SUM(A1)" in names
    assert '=HYPERLINK("http://example.invalid","click")' in names


@pytest.mark.parametrize("language", ["ar", "en", "zh"])
def test_custom_workbook_has_no_formula_cells_and_keeps_visible_text(db, language):
    blob = _custom(db, language)
    assert _raw_formula_count(blob) == 0
    cells = _all_cells(blob)
    assert not [c for _, c in cells if c.data_type == "f"]
    values = {c.value for _, c in cells if isinstance(c.value, str)}
    assert "=1+1" in values and "=DEPT()" in values


def test_ordinary_text_numbers_and_dates_are_unchanged(db):
    blob = build_hr_workbook(db, date(2026, 10, 1), date(2026, 10, 31), language="en")
    workbook = load_workbook(BytesIO(blob))
    sheet = workbook["Employees"]
    rows = {row[0].value: row for row in sheet.iter_rows(min_row=2)}
    ahmed = rows["MY-0008"]
    assert ahmed[1].value == "Ahmed Ali" and ahmed[1].data_type == "s"
    assert rows["MY-0010"][1].value == "Sara 1=2"
    assert isinstance(ahmed[6].value, (int, float)) and ahmed[6].value == 1000
    assert ahmed[6].data_type == "n"
    assert ahmed[4].value is not None and ahmed[4].is_date


def test_helper_only_changes_formula_typed_cells():
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["=1+1", "+1", "plain", 5, 2.5, True, None])
    assert sheet["A1"].data_type == "f"
    changed = neutralize_formula_cells(workbook)
    assert changed == 1
    assert sheet["A1"].data_type == "s" and sheet["A1"].value == "=1+1"
    assert [c.data_type for c in sheet[1][1:6]] == ["s", "s", "n", "n", "b"]
    assert neutralize_formula_cells(workbook) == 0


def test_helper_covers_every_sheet():
    workbook = Workbook()
    workbook.active.append(["=A"])
    workbook.create_sheet("Second").append(["x", "=B()"])
    assert neutralize_formula_cells(workbook) == 2
    blob = BytesIO()
    workbook.save(blob)
    assert _raw_formula_count(blob.getvalue()) == 0
