"""Tests for the custom report builder: selection, preview data, presets and exports."""
from datetime import date
from decimal import Decimal
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.dependencies import get_current_admin
from app.main import app
from app.models import (
    AdminAccount,
    AdvanceRequest,
    AttendanceDailyRecord,
    Department,
    Employee,
    PayrollItem,
    PayrollRun,
    ReportPreset,
)
from app.report_builder import (
    DEFAULT_FIELDS,
    ReportSelection,
    build_custom_pdf,
    build_custom_workbook,
    build_report,
    preset_config,
    resolve_period,
    save_preset,
    selection_from_params,
)


def _seed(db: Session):
    reception = Department(name="قسم الاستقبال", active=True)
    security = Department(name="قسم الأمن", active=True)
    db.add_all([reception, security])
    db.flush()
    ahmed = Employee(
        code="MY-0001", full_name="أحمد علي", department_id=reception.id, position="استقبال",
        hire_date=date(2025, 5, 1), status="active", basic_salary=Decimal("6000"),
        daily_hours=Decimal("8"), nationality="egyptian", national_id="29001010101134",
    )
    sara = Employee(
        code="MY-0002", full_name="Sara Smith", department_id=reception.id, position="استقبال",
        hire_date=date(2025, 6, 1), status="active", basic_salary=Decimal("7500"),
        daily_hours=Decimal("8"), nationality="foreign", passport_number="A12345678",
    )
    mona = Employee(
        code="MY-0003", full_name="منى سيد", department_id=security.id, position="أمن",
        hire_date=date(2026, 11, 5), status="active", basic_salary=Decimal("5000"),
        daily_hours=Decimal("8"), nationality="egyptian", national_id="29002020202277",
    )
    omar = Employee(
        code="MY-0004", full_name="عمر حسن", department_id=security.id, position="أمن",
        hire_date=date(2024, 1, 1), status="archived", basic_salary=Decimal("5500"),
        daily_hours=Decimal("8"), nationality="egyptian", national_id="29003030303390",
    )
    db.add_all([ahmed, sara, mona, omar])
    db.flush()

    oct_run = PayrollRun(
        period_start=date(2026, 10, 1), period_end=date(2026, 10, 31),
        pay_date=date(2026, 11, 5), status="closed", created_by="admin",
    )
    nov_run = PayrollRun(
        period_start=date(2026, 11, 1), period_end=date(2026, 11, 30),
        pay_date=date(2026, 12, 5), status="approved", created_by="admin",
    )
    db.add_all([oct_run, nov_run])
    db.flush()

    def item(run, employee, base, bonus, sanction, advance, late_minutes=0, absence_days=0, overtime_hours=0):
        earnings = base + bonus
        deductions = sanction + advance
        return PayrollItem(
            payroll_run_id=run.id, employee_id=employee.id,
            employee_code_snapshot=employee.code, employee_name_snapshot=employee.full_name,
            department_name_snapshot=employee.department.name, position_snapshot=employee.position,
            salary_basis=base, base_salary=base, daily_hours=Decimal("8"),
            attendance_bonus=bonus, absence_days=Decimal(absence_days),
            late_minutes=late_minutes, overtime_hours=Decimal(overtime_hours),
            staff_sanction_amount=sanction, advance_deduction=advance,
            net_pay=earnings - deductions,
        )

    # October: ahmed, sara, omar (omar leaves the November run -> leaver).
    db.add_all([
        item(oct_run, ahmed, Decimal("6000"), Decimal("1000"), Decimal("200"), Decimal("100")),
        item(oct_run, sara, Decimal("7500"), Decimal("1500"), Decimal("0"), Decimal("0")),
        item(oct_run, omar, Decimal("5500"), Decimal("500"), Decimal("100"), Decimal("0")),
    ])
    # November: ahmed, sara, mona (mona is the joiner).
    db.add_all([
        item(nov_run, ahmed, Decimal("6000"), Decimal("1200"), Decimal("350"), Decimal("150"),
             late_minutes=45, absence_days=1, overtime_hours=3),
        item(nov_run, sara, Decimal("7500"), Decimal("1500"), Decimal("0"), Decimal("0")),
        item(nov_run, mona, Decimal("5000"), Decimal("600"), Decimal("250"), Decimal("0")),
    ])
    db.add_all([
        AttendanceDailyRecord(employee_id=ahmed.id, work_date=date(2026, 11, 1), worked_minutes=480, status="approved"),
        AttendanceDailyRecord(employee_id=ahmed.id, work_date=date(2026, 11, 2), worked_minutes=470, status="approved"),
        AttendanceDailyRecord(employee_id=ahmed.id, work_date=date(2026, 11, 3), status="needs_review"),
        AttendanceDailyRecord(employee_id=sara.id, work_date=date(2026, 11, 1), worked_minutes=480, status="approved"),
    ])
    db.add(AdvanceRequest(
        employee_id=ahmed.id, advance_type="regular", amount=Decimal("500"),
        salary_basis=Decimal("6000"), request_date=date(2026, 11, 17),
        payroll_month=date(2026, 11, 1), status="approved", reason="ظرف طارئ", created_by="admin",
    ))
    db.commit()
    return {"reception": reception, "security": security, "ahmed": ahmed, "sara": sara, "mona": mona, "omar": omar}


def _db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return engine


def _november_selection(**overrides):
    selection = ReportSelection(period_choice="custom", start_date=date(2026, 11, 1), end_date=date(2026, 11, 30))
    for key, value in overrides.items():
        setattr(selection, key, value)
    return selection


def test_resolve_period_boundaries():
    selection = ReportSelection(period_choice="last_month")
    assert resolve_period(selection, today=date(2026, 11, 15)) == (date(2026, 10, 1), date(2026, 10, 31))
    selection = ReportSelection(period_choice="this_month")
    assert resolve_period(selection, today=date(2026, 11, 15)) == (date(2026, 11, 1), date(2026, 11, 15))
    selection = ReportSelection(period_choice="last_3_months")
    assert resolve_period(selection, today=date(2026, 11, 15)) == (date(2026, 9, 1), date(2026, 11, 15))
    selection = ReportSelection(period_choice="custom", start_date=date(2026, 11, 10), end_date=date(2026, 11, 1))
    with pytest.raises(ValueError):
        resolve_period(selection, today=date(2026, 11, 15))


def test_payroll_result_groups_by_department_and_balances():
    engine = _db()
    try:
        with Session(engine) as db:
            _seed(db)
            result = build_report(db, _november_selection())
        assert result.title == "تقرير الرواتب"
        assert result.period_label == "نوفمبر 2026"
        assert [group["department"] for group in result.groups] == ["قسم الأمن", "قسم الاستقبال"]
        rows = [row for group in result.groups for row in group["rows"]]
        assert len(rows) == 3  # omar has no November payroll and is archived anyway
        ahmed = next(row for row in rows if row["emp_code"] == "MY-0001")
        # Each row balances: earnings - deductions (incl. advances) = net.
        assert ahmed["pay_basic"] + ahmed["pay_allowances"] - ahmed["pay_deductions"] - ahmed["pay_advances"] == ahmed["pay_net"]
        assert ahmed["pay_net"] == Decimal("6700")
        assert ahmed["emp_nationality"] == "مصري"
        assert ahmed["emp_id_proof"] == "29001010101134"
        sara = next(row for row in rows if row["emp_code"] == "MY-0002")
        assert sara["emp_nationality"] == "أجنبي"
        assert sara["emp_id_proof"] == "باسبور A12345678"
        assert ahmed["att_present"] == 2
        assert ahmed["att_late"] == Decimal("45")
        assert ahmed["att_absent"] == Decimal("1")
        assert ahmed["att_overtime"] == Decimal("3")
        assert result.totals["pay_net"] == Decimal("6700") + Decimal("9000") + Decimal("5350")
        # Breakdown explains the net from stored components.
        breakdown = dict((label, value) for label, value, _ in ahmed["breakdown"])
        assert breakdown["الأساسي بعد التناسب"] == Decimal("6000")
        assert breakdown["خصم السلفة"] == Decimal("150")
        assert breakdown["صافي المرتب"] == Decimal("6700")
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_payroll_summary_and_comparison_like_for_like():
    engine = _db()
    try:
        with Session(engine) as db:
            _seed(db)
            result = build_report(db, _november_selection())
        summary = dict(result.summary)
        assert summary["إجمالي المستحقات"] == "21,800 ج.م"
        assert summary["إجمالي الاستقطاعات"] == "750 ج.م"
        assert summary["صافي المرتبات"] == "21,050 ج.م"
        assert summary["عدد الموظفين"] == 3
        comparison = result.comparison
        assert comparison is not None
        items = {label: (current, previous, pct) for label, current, previous, pct in comparison["items"]}
        assert items["المستحقات"][1] == Decimal("22000")
        assert items["الموظفون"][0] == 3 and items["الموظفون"][1] == 3
        lfl = comparison["like_for_like"]
        assert lfl["enabled"] is True
        assert lfl["continuing_count"] == 2  # ahmed + sara
        assert lfl["continuing_current_net"] == Decimal("6700") + Decimal("9000")
        assert lfl["continuing_previous_net"] == Decimal("6700") + Decimal("9000")
        assert lfl["continuing_pct"] == "+0.0%"
        assert lfl["joiners_count"] == 1  # mona
        assert lfl["joiners_net"] == Decimal("5350")
        assert lfl["leavers_count"] == 1  # omar
        assert lfl["leavers_previous_net"] == Decimal("5900")
        assert lfl["still_active_leavers"] == 0  # omar is archived
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_like_for_like_can_be_turned_off():
    engine = _db()
    try:
        with Session(engine) as db:
            _seed(db)
            result = build_report(db, _november_selection(compare_like_for_like=False))
        assert result.comparison["like_for_like"]["enabled"] is False
        assert len(result.comparison["items"]) == 4
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_warnings_for_missing_payroll_and_unreviewed_attendance():
    engine = _db()
    try:
        with Session(engine) as db:
            seeded = _seed(db)
            # An active employee without any November payroll item.
            extra = Employee(
                code="MY-0009", full_name="خالد جديد", department_id=seeded["reception"].id,
                position="استقبال", hire_date=date(2026, 11, 1), status="active",
                basic_salary=Decimal("4000"), daily_hours=Decimal("8"),
            )
            db.add(extra)
            db.commit()
            result = build_report(db, _november_selection())
        assert any(w["kind"] == "no_payroll" and "MY-0009" in w["details"] for w in result.warnings)
        assert any(w["kind"] == "unreviewed_days" for w in result.warnings)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_field_selection_limits_columns():
    engine = _db()
    try:
        with Session(engine) as db:
            _seed(db)
            result = build_report(db, _november_selection(fields={"emp_name", "pay_net"}))
        assert [key for key, _, _ in result.columns] == ["emp_name", "pay_net"]
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_advances_report_rows_and_status_labels():
    engine = _db()
    try:
        with Session(engine) as db:
            _seed(db)
            selection = _november_selection(report_type="advances")
            result = build_report(db, selection)
        rows = [row for group in result.groups for row in group["rows"]]
        assert len(rows) == 1
        assert rows[0]["amount"] == Decimal("500")
        assert rows[0]["status"] == "معتمدة"
        assert rows[0]["advance_type"] == "شهرية"
        assert dict(result.summary)["إجمالي المبالغ"] == "500 ج.م"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_custom_workbook_and_pdf_match_selection():
    engine = _db()
    try:
        with Session(engine) as db:
            _seed(db)
            selection = _november_selection()
            workbook_bytes = build_custom_workbook(db, selection)
            pdf_bytes = build_custom_pdf(db, selection)
        workbook = load_workbook(BytesIO(workbook_bytes), read_only=False, data_only=True)
        try:
            assert "التقرير" in workbook.sheetnames
            assert "الملخص" in workbook.sheetnames
            assert "المقارنة" in workbook.sheetnames
            assert "تفصيل الحساب" in workbook.sheetnames
            assert "بيانات الاستخراج" in workbook.sheetnames
        finally:
            workbook.close()
        assert pdf_bytes.startswith(b"%PDF-")
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_preset_round_trip_and_update():
    engine = _db()
    try:
        with Session(engine) as db:
            _seed(db)
            config = _november_selection().to_config()
            preset = save_preset(db, "كشف صرف المرتبات الشهري", config, "admin")
            db.commit()
            loaded = db.get(ReportPreset, preset.id)
            restored = ReportSelection.from_config(preset_config(loaded))
            assert restored.report_type == "payroll"
            assert restored.fields == DEFAULT_FIELDS
            assert restored.compare_like_for_like is True
            # Same name updates instead of duplicating.
            updated = save_preset(db, "كشف صرف المرتبات الشهري", _november_selection(report_type="attendance").to_config(), "admin")
            db.commit()
            assert updated.id == preset.id
            assert db.query(ReportPreset).count() == 1
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _route_client(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    with factory.begin() as db:
        _seed(db)
    app.dependency_overrides[get_current_admin] = lambda: AdminAccount(
        username="report-builder-admin", active=True
    )
    monkeypatch.setattr("app.main.SessionLocal", factory)
    return engine


def test_reports_request_page_shows_new_controls(monkeypatch):
    engine = _route_client(monkeypatch)
    try:
        with TestClient(app) as client:
            response = client.get("/reports")
        assert response.status_code == 200
        assert 'name="report_type"' in response.text
        assert 'name="fields"' in response.text
        assert 'name="lfl"' in response.text
        assert "إعداد تقرير جديد" in response.text
        assert "فصل المنضمين والمغادرين" in response.text
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_report_view_renders_grouped_table_with_comparison(monkeypatch):
    engine = _route_client(monkeypatch)
    params = {
        "report_type": "payroll", "period": "custom",
        "start_date": "2026-11-01", "end_date": "2026-11-30",
        "fields": ["emp_name", "emp_code", "pay_basic", "pay_net"], "lfl": "1",
    }
    try:
        with TestClient(app) as client:
            response = client.get("/reports/view", params=params)
        assert response.status_code == 200
        assert "تقرير الرواتب" in response.text and "نوفمبر" in response.text and "2026" in response.text
        assert "قسم الاستقبال" in response.text
        assert "مقارنة بالفترة السابقة" in response.text
        assert "مثل بمثل" in response.text
        assert "كيف تم التجميع؟" in response.text
        assert "6,700" in response.text
        assert response.text.count('class="breakdown-table paper-breakdown"') == 3
        assert response.text.count('class="employee-main-row"') == 3
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_custom_excel_export_route_uses_selection(monkeypatch, tmp_path):
    engine = _route_client(monkeypatch)
    monkeypatch.setattr("app.main.REPORTS_DIR", tmp_path / "reports")
    params = {
        "report_type": "payroll", "period": "custom",
        "start_date": "2026-11-01", "end_date": "2026-11-30",
        "fields": ["emp_name", "pay_net"],
    }
    try:
        with TestClient(app) as client:
            excel = client.get("/reports/export.xlsx", params=params)
            pdf = client.get("/reports/export.pdf", params=params)
        assert excel.status_code == 200 and excel.content.startswith(b"PK")
        workbook = load_workbook(BytesIO(excel.content), read_only=False, data_only=True)
        try:
            assert "التقرير" in workbook.sheetnames
            header = [cell.value for cell in workbook["التقرير"][4]]
            assert header == ["اسم الموظف", "صافي المرتب"]
        finally:
            workbook.close()
        assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF-")
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_preset_save_and_apply_via_routes(monkeypatch):
    engine = _route_client(monkeypatch)
    try:
        with TestClient(app) as client:
            page = client.get("/reports")
            import re

            token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
            saved = client.post(
                "/reports/presets",
                data={
                    "csrf_token": token,
                    "preset_name": "كشف شهري",
                    "report_type": "payroll",
                    "period": "this_month",
                    "employee_status": "active",
                    "lfl": "1",
                    "fields": ["emp_name", "pay_net"],
                },
                follow_redirects=False,
            )
            assert saved.status_code == 303
            assert "preset_id=" in saved.headers["location"]
            preset_id = saved.headers["location"].split("preset_id=")[1]
            applied = client.get(f"/reports?preset_id={preset_id}")
            assert applied.status_code == 200
            assert "كشف شهري" in applied.text
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_comprehensive_view_redirects_back_with_note(monkeypatch):
    engine = _route_client(monkeypatch)
    params = {"report_type": "comprehensive", "period": "this_month"}
    try:
        with TestClient(app) as client:
            response = client.get("/reports/view", params=params, follow_redirects=False)
        assert response.status_code == 303
        assert "error=comprehensive" in response.headers["location"]
    finally:
        app.dependency_overrides.clear()
        Base.metadata.drop_all(engine)
        engine.dispose()


@pytest.mark.parametrize("enabled", [False, True])
def test_custom_exports_respect_optional_like_for_like(monkeypatch, enabled):
    """User option must control existing output, not just model.enabled."""
    from app import reports as pdf_helpers
    engine = _db()
    observed = []
    original = pdf_helpers._pdf_cell

    def record(value):
        observed.append(str(value))
        return original(value)

    monkeypatch.setattr(pdf_helpers, "_pdf_cell", record)
    try:
        with Session(engine) as db:
            _seed(db)
            selection = _november_selection(compare_like_for_like=enabled)
            result = build_report(db, selection)
            pdf = build_custom_pdf(db, selection)
            workbook = load_workbook(BytesIO(build_custom_workbook(db, selection)), read_only=True, data_only=True)
        try:
            comparison_cells = [str(v) for row in workbook["المقارنة"].values for v in row if v is not None]
        finally:
            workbook.close()
        assert pdf.startswith(b"%PDF-")
        assert any("مقارنة مثل بمثل مع الفترة السابقة" in text for text in observed) is enabled
        assert any("مقارنة مثل بمثل (المستمرون في الفترتين)" in text for text in comparison_cells) is enabled
        assert any("منضمون في الفترة الحالية" in text for text in observed) is enabled
        assert any("منضمون في الفترة الحالية" in text for text in comparison_cells) is enabled
        # Disabling optional membership separation preserves ordinary comparison
        # and current-period payroll values instead of dropping all comparison.
        assert len(result.comparison["items"]) == 4
        assert "الفترة الحالية" in comparison_cells
        assert dict(result.summary)["عدد الموظفين"] == 3
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_paper_content_scoped_components_and_signatures():
    from app.report_builder import report_paper_content, PAYROLL_BREAKDOWN_DEFS
    engine = _db()
    try:
        with Session(engine) as db:
            _seed(db)
            result = build_report(db, _november_selection(compare_like_for_like=False))
            paper = report_paper_content(result)
            assert paper["signatures"] == ("إعداد", "مراجعة")
            assert paper["like_for_like"] is None
            assert len(paper["comparison_items"]) == 4
            assert len(paper["components_by_employee"]) == 3
            for components in paper["components_by_employee"].values():
                assert [label for label, _, _ in components] == [label for _, label, _ in PAYROLL_BREAKDOWN_DEFS]
                assert len(components) == 16
            selected = next(iter(paper["components_by_employee"]))
            scoped = report_paper_content(build_report(db, _november_selection(employee_id=selected)))
            assert set(scoped["components_by_employee"]) == {selected}
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
