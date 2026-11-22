"""Local Excel reports for HR, attendance, leave, advances, staff actions and payroll."""
from datetime import date, datetime, time, timedelta
from io import BytesIO

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.models import CAIRO_TZ
from app.models import (
    AdvanceRequest,
    AttendanceDailyRecord,
    Department,
    Employee,
    LeaveBalanceTransaction,
    LeaveRecord,
    LeaveType,
    PayrollItem,
    PayrollPayment,
    PayrollRun,
    StaffAction,
)


def _filtered_date(column, start_date: date, end_date: date):
    return (column >= start_date, column <= end_date)


def _excel_datetime(value):
    """Remove timezone metadata from datetimes before writing Excel cells."""
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def _write_sheet(workbook: Workbook, title: str, headers: list[str], rows: list[tuple]) -> None:
    sheet = workbook.create_sheet(title=title[:31])
    sheet.append(headers)
    for row in rows:
        sheet.append(list(row))
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.row_dimensions[1].height = 28
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="24415E")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    for column_cells in sheet.columns:
        max_length = max((len(str(cell.value or "")) for cell in column_cells), default=10)
        sheet.column_dimensions[get_column_letter(column_cells[0].column)].width = min(max(max_length + 2, 12), 34)
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)


def build_hr_workbook(
    db: Session,
    start_date: date,
    end_date: date,
    employee_id: int | None = None,
    language: str = "en",
) -> bytes:
    if end_date < start_date:
        raise ValueError("End date must not precede start date.")

    workbook = Workbook()
    workbook.remove(workbook.active)

    employee_query = select(Employee).order_by(Employee.code)
    if employee_id is not None:
        employee_query = employee_query.where(Employee.id == employee_id)
    employees = db.scalars(employee_query).all()
    if employee_id is not None and not employees:
        raise ValueError("Selected employee was not found.")
    _write_sheet(
        workbook,
        "Employees",
        ["Employee code", "Full name", "Department", "Position", "Hire date", "Status", "Basic salary", "Daily hours", "Phone"],
        [
            (
                employee.code,
                employee.full_name,
                employee.department.name if employee.department else "",
                employee.position,
                employee.hire_date,
                employee.status,
                float(employee.basic_salary),
                float(employee.daily_hours),
                employee.phone or "",
            )
            for employee in employees
        ],
    )

    attendance = db.scalars(
        select(AttendanceDailyRecord)
        .where(*_filtered_date(AttendanceDailyRecord.work_date, start_date, end_date))
        .where(AttendanceDailyRecord.employee_id == employee_id if employee_id is not None else True)
        .order_by(AttendanceDailyRecord.work_date, AttendanceDailyRecord.employee_id)
    ).all()
    employee_map = {employee.id: employee for employee in employees}
    _write_sheet(
        workbook,
        "Attendance",
        ["Work date", "Employee code", "Employee", "Status", "Scheduled in", "Scheduled out", "Approved in", "Approved out", "Worked minutes", "Review notes"],
        [
            (
                record.work_date,
                employee_map[record.employee_id].code if record.employee_id in employee_map else "",
                employee_map[record.employee_id].full_name if record.employee_id in employee_map else "",
                record.status,
                record.scheduled_start_at,
                record.scheduled_end_at,
                record.approved_clock_in,
                record.approved_clock_out,
                record.worked_minutes,
                record.review_notes or "",
            )
            for record in attendance
        ],
    )

    leave_records = db.scalars(
        select(LeaveRecord)
        .where(LeaveRecord.start_date <= end_date, LeaveRecord.end_date >= start_date)
        .where(LeaveRecord.employee_id == employee_id if employee_id is not None else True)
        .order_by(LeaveRecord.start_date, LeaveRecord.employee_id)
    ).all()
    leave_types = {
        item.id: item.name_ar
        for item in db.scalars(select(LeaveType)).all()
    }
    _write_sheet(
        workbook,
        "Leave",
        ["Employee code", "Employee", "Leave type", "Start", "End", "Calendar days", "Charged days", "Status", "Reason"],
        [
            (
                employee_map[item.employee_id].code if item.employee_id in employee_map else "",
                employee_map[item.employee_id].full_name if item.employee_id in employee_map else "",
                leave_types.get(item.leave_type_id, ""),
                item.start_date,
                item.end_date,
                item.calendar_days,
                float(item.charged_days),
                item.status,
                item.reason,
            )
            for item in leave_records
        ],
    )

    balance_transactions = db.scalars(
        select(LeaveBalanceTransaction)
        .where(LeaveBalanceTransaction.created_at >= datetime.combine(start_date, time.min, tzinfo=CAIRO_TZ), LeaveBalanceTransaction.created_at < datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=CAIRO_TZ))
        .where(LeaveBalanceTransaction.employee_id == employee_id if employee_id is not None else True)
        .order_by(LeaveBalanceTransaction.created_at, LeaveBalanceTransaction.id)
    ).all()
    _write_sheet(
        workbook,
        "Leave balances",
        ["Date", "Employee code", "Employee", "Leave type ID", "Transaction", "Days delta", "Reason", "Recorded by"],
        [
            (
                _excel_datetime(item.created_at),
                employee_map[item.employee_id].code if item.employee_id in employee_map else "",
                employee_map[item.employee_id].full_name if item.employee_id in employee_map else "",
                item.leave_type_id,
                item.transaction_type,
                float(item.days_delta),
                item.reason,
                item.created_by,
            )
            for item in balance_transactions
        ],
    )

    actions = db.scalars(
        select(StaffAction)
        .where(StaffAction.created_at >= datetime.combine(start_date, time.min, tzinfo=CAIRO_TZ), StaffAction.created_at < datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=CAIRO_TZ))
        .where(StaffAction.employee_id == employee_id if employee_id is not None else True)
        .order_by(StaffAction.created_at, StaffAction.id)
    ).all()
    _write_sheet(
        workbook,
        "Sanctions rewards",
        ["Date", "Employee code", "Employee", "Type", "Category", "Title", "Financial", "Amount", "Payroll month", "Status", "Reason"],
        [
            (
                _excel_datetime(item.created_at),
                employee_map[item.employee_id].code if item.employee_id in employee_map else "",
                employee_map[item.employee_id].full_name if item.employee_id in employee_map else "",
                item.kind,
                item.category,
                item.title,
                item.is_financial,
                float(item.amount) if item.amount is not None else None,
                item.payroll_month,
                item.status,
                item.reason,
            )
            for item in actions
        ],
    )

    advances = db.scalars(
        select(AdvanceRequest)
        .where(*_filtered_date(AdvanceRequest.request_date, start_date, end_date))
        .where(AdvanceRequest.employee_id == employee_id if employee_id is not None else True)
        .order_by(AdvanceRequest.request_date, AdvanceRequest.employee_id)
    ).all()
    _write_sheet(
        workbook,
        "Advances",
        ["Request date", "Employee code", "Employee", "Payroll month", "Amount", "Salary basis", "Status", "Paid at", "Deducted at", "Reason"],
        [
            (
                item.request_date,
                employee_map[item.employee_id].code if item.employee_id in employee_map else "",
                employee_map[item.employee_id].full_name if item.employee_id in employee_map else "",
                item.payroll_month,
                float(item.amount),
                float(item.salary_basis),
                item.status,
                _excel_datetime(item.paid_at),
                _excel_datetime(item.deducted_at),
                item.reason,
            )
            for item in advances
        ],
    )

    payroll_rows = db.execute(
        select(PayrollRun, PayrollItem, Employee)
        .join(PayrollItem, PayrollItem.payroll_run_id == PayrollRun.id)
        .join(Employee, Employee.id == PayrollItem.employee_id)
        .where(PayrollRun.period_start <= end_date, PayrollRun.period_end >= start_date)
        .where(PayrollItem.employee_id == employee_id if employee_id is not None else True)
        .order_by(PayrollRun.period_start, Employee.code)
    ).all()
    payment_sums = dict(
        db.execute(
            select(PayrollPayment.payroll_item_id, func.coalesce(func.sum(PayrollPayment.amount), 0))
            # Superseded payments were corrected away; only active records count,
            # matching payment_totals() everywhere else.
            .where(PayrollPayment.superseded_by_payment_id.is_(None))
            .group_by(PayrollPayment.payroll_item_id)
        ).all()
    )
    _write_sheet(
        workbook,
        "Payroll",
        ["Month", "Pay date", "Run status", "Employee code", "Employee", "Basic", "Base after proration", "Attendance bonus", "Performance bonus", "Occupancy bonus", "Overtime", "Other bonus", "Absence deduction", "Lateness deduction", "Insurance", "Taxes", "Other deductions", "Advance", "Financial reward", "Financial sanction", "Net pay", "Paid", "Remaining"],
        [
            (
                run.period_start,
                run.pay_date,
                run.status,
                item.employee_code_snapshot,
                item.employee_name_snapshot,
                float(item.salary_basis),
                float(item.base_salary),
                float(item.attendance_bonus),
                float(item.performance_bonus),
                float(item.occupancy_bonus),
                float(item.overtime_amount),
                float(item.other_bonus),
                float(item.absence_deduction),
                float(item.lateness_deduction),
                float(item.insurance_share),
                float(item.taxes),
                float(item.other_deductions),
                float(item.advance_deduction),
                float(item.staff_reward_amount),
                float(item.staff_sanction_amount),
                float(item.net_pay),
                float(payment_sums.get(item.id, 0)),
                float(max(0, item.net_pay - payment_sums.get(item.id, 0))),
            )
            for run, item, employee in payroll_rows
        ],
    )

    department_rows = db.execute(
        select(
            Department.name,
            func.count(Employee.id),
            func.coalesce(func.sum(Employee.basic_salary), 0),
        )
        .outerjoin(Employee, and_(Employee.department_id == Department.id, Employee.status == "active"))
        .group_by(Department.id, Department.name)
        .order_by(Department.name)
    ).all()
    _write_sheet(
        workbook,
        "Departments",
        ["Department", "Employee count", "Current monthly basic payroll"],
        [(name, count, float(total)) for name, count, total in department_rows],
    )

    for sheet in workbook.worksheets:
        if language != "en":
            sheet.title = _legacy_L(sheet.title, language)[:31]
            sheet.sheet_view.rightToLeft = language == "ar"
            for header_cell in sheet[1]:
                if isinstance(header_cell.value, str):
                    header_cell.value = _legacy_L(header_cell.value, language)
        for data_row in sheet.iter_rows(min_row=2):
            for data_cell in data_row:
                if isinstance(data_cell.value, str):
                    data_cell.value = _legacy_enum(data_cell.value, language)

    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def validate_workbook(data: bytes) -> list[str]:
    workbook = load_workbook(BytesIO(data), read_only=True, data_only=True)
    try:
        return workbook.sheetnames
    finally:
        workbook.close()



def _pdf_cell_mixed(value, arabic_font: str) -> str:
    """PDF cell for CJK documents: Arabic data runs use the Arabic font inside the CJK one."""
    import re as _re
    from xml.sax.saxutils import escape
    text = "" if value is None else str(value)
    if not any("\u0600" <= char <= "\u06ff" for char in text):
        return escape(text).replace("\n", "<br/>")
    parts = _re.split(r"([\u0600-\u06ff][\u0600-\u06ff\s،؛.-]*)", text)
    rendered = []
    for part in parts:
        if part and any("\u0600" <= c <= "\u06ff" for c in part):
            rendered.append(f'<font name="{arabic_font}">{_pdf_cell(part)}</font>')
        elif part:
            rendered.append(escape(part))
    return "".join(rendered)


_LEGACY_LABELS = {
    "Employees": ("الموظفون", "员工"),
    "Attendance": ("الحضور", "考勤"),
    "Leave": ("الإجازات", "请假"),
    "Leave balances": ("أرصدة الإجازات", "请假余额"),
    "Sanctions rewards": ("الجزاءات والمكافآت", "奖惩"),
    "Advances": ("السلف", "预支款"),
    "Payroll": ("الرواتب", "工资"),
    "Departments": ("الأقسام", "部门"),
    "Employee code": ("رقم الموظف", "员工编号"),
    "Full name": ("الاسم الكامل", "全名"),
    "Department": ("القسم", "部门"),
    "Position": ("المسمى الوظيفي", "职位"),
    "Hire date": ("تاريخ التعيين", "入职日期"),
    "Status": ("الحالة", "状态"),
    "Basic salary": ("الراتب الأساسي", "基本工资"),
    "Daily hours": ("ساعات العمل اليومية", "每日工时"),
    "Phone": ("الهاتف", "电话"),
    "Work date": ("تاريخ العمل", "工作日期"),
    "Employee": ("الموظف", "员工"),
    "Scheduled in": ("الحضور المجدول", "应到时间"),
    "Scheduled out": ("الانصراف المجدول", "应离时间"),
    "Approved in": ("الحضور المعتمد", "实到时间"),
    "Approved out": ("الانصراف المعتمد", "实离时间"),
    "Worked minutes": ("دقائق العمل", "工作分钟数"),
    "Review notes": ("ملاحظات المراجعة", "审核备注"),
    "Leave type": ("نوع الإجازة", "请假类型"),
    "Start": ("البداية", "开始"),
    "End": ("النهاية", "结束"),
    "Calendar days": ("أيام التقويم", "日历天数"),
    "Charged days": ("الأيام المحتسبة", "计扣天数"),
    "Reason": ("السبب", "原因"),
    "Date": ("التاريخ", "日期"),
    "Leave type ID": ("رقم نوع الإجازة", "请假类型编号"),
    "Transaction": ("الحركة", "交易"),
    "Days delta": ("تغير الأيام", "天数变动"),
    "Recorded by": ("سُجل بواسطة", "记录人"),
    "Type": ("النوع", "类型"),
    "Category": ("التصنيف", "类别"),
    "Title": ("العنوان", "标题"),
    "Financial": ("مالي", "是否涉款"),
    "Amount": ("المبلغ", "金额"),
    "Payroll month": ("شهر الراتب", "工资月份"),
    "Request date": ("تاريخ الطلب", "申请日期"),
    "Salary basis": ("أساس الاحتساب", "计算基数"),
    "Paid at": ("وقت الصرف", "发放时间"),
    "Deducted at": ("وقت الخصم", "扣款时间"),
    "Month": ("الشهر", "月份"),
    "Pay date": ("تاريخ الصرف", "发放日期"),
    "Run status": ("حالة المسير", "工资单状态"),
    "Basic": ("الأساسي", "基本工资"),
    "Base after proration": ("الأساسي بعد التناسب", "按比例折算后基本工资"),
    "Attendance bonus": ("بدل حضور", "出勤津贴"),
    "Performance bonus": ("بدل أداء", "绩效津贴"),
    "Occupancy bonus": ("بدل إشغال", "入住率津贴"),
    "Overtime": ("إضافي", "加班"),
    "Other bonus": ("بدلات أخرى", "其他奖金"),
    "Absence deduction": ("خصم غياب", "缺勤扣款"),
    "Lateness deduction": ("خصم تأخير", "迟到扣款"),
    "Insurance": ("تأمينات", "社保"),
    "Taxes": ("ضرائب", "税费"),
    "Other deductions": ("خصومات أخرى", "其他扣款"),
    "Advance": ("خصم السلفة", "预支扣款"),
    "Financial reward": ("مكافأة مالية", "奖金"),
    "Financial sanction": ("جزاء مالي", "罚款"),
    "Net pay": ("صافي المرتب", "实发工资"),
    "Paid": ("المصروف", "已发放"),
    "Remaining": ("المتبقي", "剩余"),
    "Employee count": ("عدد الموظفين", "员工数"),
    "Current monthly basic payroll": ("إجمالي الرواتب الأساسية الشهرية الحالية", "当前月基本工资总额"),
}


# Internal status/type codes stored in the database. Exports translate the
# display value only; stored values are never changed.
_LEGACY_ENUMS = {
    "active": ("نشط", "Active", "在职"),
    "archived": ("مؤرشف", "Archived", "已存档"),
    "draft": ("مسودة", "Draft", "草稿"),
    "approved": ("معتمد", "Approved", "已批准"),
    "pending": ("معلق", "Pending", "待处理"),
    "rejected": ("مرفوض", "Rejected", "已拒绝"),
    "cancelled": ("ملغي", "Cancelled", "已取消"),
    "closed": ("مغلق", "Closed", "已关闭"),
    "reopened": ("معاد فتحه", "Reopened", "已重新打开"),
    "under_review": ("قيد المراجعة", "Under Review", "审核中"),
    "needs_review": ("يحتاج مراجعة", "Needs Review", "需要审核"),
    "ready_for_review": ("جاهز للمراجعة", "Ready for Review", "待审核"),
    "success": ("ناجح", "Successful", "成功"),
    "failed": ("فاشل", "Failed", "失败"),
    "regular": ("عادية", "Regular", "普通"),
    "installment": ("مقسطة", "Installment", "分期"),
    "sanction": ("جزاء", "Sanction", "处罚"),
    "reward": ("مكافأة", "Reward", "奖励"),
    "viewed": ("تم الاطلاع", "Viewed", "已告知"),
    "declined": ("مرفوض", "Declined", "拒绝确认"),
    "unpaid": ("غير مدفوع", "Unpaid", "未支付"),
    "partially_paid": ("مدفوع جزئيًا", "Partially Paid", "部分支付"),
    "paid": ("مدفوع", "Paid", "已支付"),
    "deducted": ("مخصومة", "Deducted", "已扣除"),
    "confirmed": ("مؤكدة", "Confirmed", "已确认"),
    "scheduled": ("مجدول", "Scheduled", "已排班"),
    "imported": ("مستورد", "Imported", "已导入"),
    "uploaded": ("مرفوع", "Uploaded", "已上传"),
    "applied": ("مطبَّق", "Applied", "已应用"),
    "present": ("حاضر", "Present", "出勤"),
    "absent": ("غائب", "Absent", "缺勤"),
}
_LEGACY_ENUM_INDEX = {"ar": 0, "en": 1, "zh": 2}


def _legacy_enum(value: str, language: str) -> str:
    """Translate a stored status/type code for display; anything else passes through."""
    translations = _LEGACY_ENUMS.get(value)
    if translations is None:
        return value
    return translations[_LEGACY_ENUM_INDEX.get(language, 1)]


def _legacy_L(text: str, language: str) -> str:
    """Translate a legacy export label (authored in English) to ar/zh; en stays as authored."""
    if language == "en" or not isinstance(text, str):
        return text
    pair = _LEGACY_LABELS.get(text)
    if pair is None:
        return text
    return pair[0] if language == "ar" else pair[1]


def _pdf_font_name() -> str:
    from pathlib import Path
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/tahoma.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf"),
    ]
    for font_path in candidates:
        if font_path.is_file():
            font_name = "MYHotelArabic"
            if font_name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(font_name, str(font_path)))
            return font_name
    raise RuntimeError("An Arabic-capable system font is required to create the PDF report.")


def _pdf_cell(value, *, reshape_arabic: bool = True) -> str:
    from xml.sax.saxutils import escape
    text = "" if value is None else str(value)
    if reshape_arabic and any("\u0600" <= char <= "\u06ff" for char in text):
        import arabic_reshaper
        from bidi.algorithm import get_display
        reshaper = arabic_reshaper.ArabicReshaper(configuration={"support_ligatures": False})
        text = get_display(reshaper.reshape(text))
    return escape(text).replace("\n", "<br/>")


def build_hr_pdf(
    db: Session,
    start_date: date,
    end_date: date,
    employee_id: int | None = None,
    language: str = "en",
) -> bytes:
    """Build a printable PDF companion to the complete Excel workbook, in the admin's language."""
    if end_date < start_date:
        raise ValueError("End date must not precede start date.")

    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A3, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import LongTable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    arabic_font = _pdf_font_name()
    if language == "zh":
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        if "STSong-Light" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        font_name = "STSong-Light"
    else:
        font_name = arabic_font
    if language == "zh":
        def cell(value):
            return _pdf_cell_mixed(value, arabic_font)
    else:
        cell = _pdf_cell
    workbook = load_workbook(BytesIO(build_hr_workbook(db, start_date, end_date, employee_id=employee_id, language=language)), read_only=True, data_only=True)
    output = BytesIO()
    page_size = landscape(A3)
    document = SimpleDocTemplate(
        output,
        pagesize=page_size,
        rightMargin=12 * mm,
        leftMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
        title=f"M.Y. Business Hotel HR Report {start_date} - {end_date}",
        author="M.Y. Business Hotel HR System",
    )
    base_styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "MYReportTitle", parent=base_styles["Title"], fontName=font_name,
        fontSize=16, leading=20, alignment=TA_RIGHT, spaceAfter=5 * mm,
    )
    section_style = ParagraphStyle(
        "MYReportSection", parent=base_styles["Heading2"], fontName=font_name,
        fontSize=11, leading=14, alignment=TA_RIGHT, spaceBefore=3 * mm, spaceAfter=2 * mm,
    )
    cell_style = ParagraphStyle(
        "MYReportCell", parent=base_styles["BodyText"], fontName=font_name,
        fontSize=6, leading=7.2, alignment=TA_RIGHT, wordWrap="CJK",
    )
    header_style = ParagraphStyle(
        "MYReportHeader", parent=cell_style, fontSize=6.5, leading=8,
        alignment=TA_CENTER, textColor=colors.white,
    )

    if language == "en":
        intro_title = "M.Y. Business Hotel — HR Report"
        intro_period = f"Period from {start_date.strftime('%d/%m/%Y')} to {end_date.strftime('%d/%m/%Y')}"
        intro_note = "This report is for viewing and printing only; it does not change any data in the system."
    elif language == "zh":
        intro_title = "M.Y. Business Hotel — 人力资源报表"
        intro_period = f"期间：{start_date.strftime('%d/%m/%Y')} 至 {end_date.strftime('%d/%m/%Y')}"
        intro_note = "本报表仅供查看和打印，不会更改系统中的任何数据。"
    else:
        intro_title = "M.Y. Business Hotel — تقرير الموارد البشرية"
        intro_period = f"الفترة من {start_date.strftime('%d/%m/%Y')} إلى {end_date.strftime('%d/%m/%Y')}"
        intro_note = "هذا التقرير للعرض والطباعة فقط، ولا يغيّر أي بيانات داخل النظام."
    story = [
        Paragraph(cell(intro_title), title_style),
        Paragraph(cell(intro_period), section_style),
        Paragraph(cell(intro_note), cell_style),
        Spacer(1, 4 * mm),
    ]
    for sheet_index, sheet in enumerate(workbook.worksheets):
        rows = list(sheet.iter_rows(values_only=True))
        if not rows:
            continue
        story.append(Paragraph(cell(sheet.title), section_style))
        formatted = []
        for row_index, row in enumerate(rows):
            style = header_style if row_index == 0 else cell_style
            formatted.append([Paragraph(cell(value), style) for value in row])
        col_count = max(1, len(rows[0]))
        available_width = page_size[0] - document.leftMargin - document.rightMargin
        widths = [available_width / col_count] * col_count
        table = LongTable(
            formatted,
            colWidths=widths,
            repeatRows=1,
            hAlign="RIGHT",
            splitByRow=1,
        )
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#24415E")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAB4BE")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), 2),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        story.append(table)
        if sheet_index < len(workbook.worksheets) - 1:
            story.append(PageBreak())
    workbook.close()
    document.build(story)
    return output.getvalue()
