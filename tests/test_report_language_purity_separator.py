# ruff: noqa: F811
"""Language purity: list separators in reports must not be the Arabic comma in EN/ZH."""
import shutil
import subprocess
from datetime import date
from decimal import Decimal

import pytest

from app.models import Department, Employee
from tests.test_payroll_routes import (  # noqa: F401  (fixture re-export)
    create_test_payroll_run, login, payroll_client)

ARABIC_COMMA = "\u060c"
Q = "start_date=2026-10-01&end_date=2026-10-31"


def _pdf_text(content, tmp_path, name):
    path = tmp_path / name
    path.write_bytes(content)
    return subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


@pytest.mark.skipif(shutil.which("pdftotext") is None, reason="UNRAN: pdftotext (poppler) not installed on this runner")
@pytest.mark.parametrize("lang,sep", [("en", ", "), ("zh", "\u3001")])
def test_report_list_separators_are_language_pure(payroll_client, tmp_path, lang, sep):
    c = payroll_client
    login(c)
    create_test_payroll_run(c)
    with c.test_session_factory.begin() as db:
        dept = db.query(Department).first().id
        for n in (2, 3):  # active employees without payroll feed the "no payroll" warning list
            db.add(Employee(code=f"MY-TEST-NOPAY-{n:04d}", full_name=f"No Pay {n}", position="Test", department_id=dept,
                            hire_date=date(2026, 10, 1), status="active",
                            basic_salary=Decimal("5000.00"), daily_hours=Decimal("8")))
    c.cookies.set("my_hr_language", lang)

    monthly = c.get("/reports/monthly.pdf?month=2026-10&" + "&".join(f"opt={i}" for i in range(1, 12)))
    assert monthly.status_code == 200
    text = _pdf_text(monthly.content, tmp_path, "m.pdf")
    assert ARABIC_COMMA not in text
    assert sep in text  # the "selected sections" line really uses the localized separator

    view = c.get(f"/reports/view?report_type=payroll&period=custom&{Q}")
    assert view.status_code == 200
    assert "MY-TEST-NOPAY-0002" in view.text
    assert ARABIC_COMMA not in view.text

    custom = c.get(f"/reports/export.pdf?report_type=payroll&period=custom&{Q}")
    assert custom.status_code == 200
    assert ARABIC_COMMA not in _pdf_text(custom.content, tmp_path, "c.pdf")


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_monthly_role_rejection_message_is_language_pure(payroll_client, lang):
    from app.models import AdminAccount
    c = payroll_client
    with c.test_session_factory() as db:
        db.execute(AdminAccount.__table__.update().values(role="assistant_manager"))
        db.commit()
    c.cookies.clear()
    login(c)
    c.cookies.set("my_hr_language", lang)
    r = c.get("/reports/monthly.pdf?month=2026-10&opt=1&opt=2&opt=3")
    assert r.status_code == 403
    assert not any("\u0600" <= ch <= "\u06ff" for ch in r.json()["detail"])


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_custom_export_foreign_passport_and_meta_label_are_language_pure(payroll_client, tmp_path, lang):
    """Foreign-staff id proof cell ("باسبور <no>") and the xlsx meta-sheet label must not stay Arabic."""
    import io
    import openpyxl
    c = payroll_client
    login(c)
    create_test_payroll_run(c)
    with c.test_session_factory.begin() as db:
        emp = db.query(Employee).first()
        emp.nationality = "foreign"
        emp.passport_number = "X1234567"
    c.cookies.set("my_hr_language", lang)
    r = c.get(f"/reports/export.xlsx?report_type=payroll&period=custom&{Q}&fields=emp_code&fields=emp_name&fields=emp_id_proof")
    assert r.status_code == 200, r.text[:200]
    cells = []
    for ws in openpyxl.load_workbook(io.BytesIO(r.content)):
        cells.append(ws.title)
        for row in ws.iter_rows(values_only=True):
            cells += [str(v) for v in row if v is not None]
    assert any("X1234567" in v for v in cells)
    assert [v for v in cells if any("\u0600" <= ch <= "\u06ff" for ch in v)] == []
