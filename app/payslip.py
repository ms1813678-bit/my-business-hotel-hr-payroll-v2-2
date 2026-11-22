"""Per-employee payslip (قسيمة مرتب) as a printable one-page PDF."""
from __future__ import annotations

from io import BytesIO

from app.report_builder import _L, _NumberedCanvas, _fmt_number


def _zero(value) -> bool:
    return value is None or float(value) == 0.0


def build_payslip_pdf(run, item, payment_totals, generated_at, language: str = "ar") -> bytes:
    """Render one employee's payroll item as an A4 portrait payslip.

    ``payment_totals`` is the (paid, remaining, status) triple from
    payroll_workflow.payment_totals. All labels are authored in Arabic and
    translated through the i18n catalogs via ``_L``.
    """
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import LongTable, Paragraph, SimpleDocTemplate, Spacer, TableStyle

    from app.reports import _pdf_cell, _pdf_font_name

    def L(text):
        return _L(text, language)

    if language == "zh":
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        if "STSong-Light" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        font_name = "STSong-Light"
        from app.reports import _pdf_cell_mixed
        arabic_font = _pdf_font_name()

        def cell_(value):
            return _pdf_cell_mixed(value, arabic_font)
    else:
        font_name = _pdf_font_name()
        cell_ = _pdf_cell

    base_styles = getSampleStyleSheet()
    title_style = ParagraphStyle("PSTitle", parent=base_styles["Title"], fontName=font_name, fontSize=16, leading=20, alignment=TA_CENTER, spaceAfter=2 * mm)
    meta_style = ParagraphStyle("PSMeta", parent=base_styles["BodyText"], fontName=font_name, fontSize=9, leading=12, alignment=TA_CENTER)
    section_style = ParagraphStyle("PSSection", parent=base_styles["BodyText"], fontName=font_name, fontSize=11, leading=14, alignment=TA_RIGHT, spaceBefore=4 * mm, spaceAfter=1.5 * mm)
    cell_style = ParagraphStyle("PSCell", parent=base_styles["BodyText"], fontName=font_name, fontSize=9.5, leading=12.5, alignment=TA_RIGHT, wordWrap="CJK")
    header_style = ParagraphStyle("PSHeader", parent=cell_style, fontSize=10, leading=13, alignment=TA_CENTER, textColor=colors.white)

    output = BytesIO()
    document = SimpleDocTemplate(
        output,
        pagesize=A4,
        rightMargin=16 * mm,
        leftMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=14 * mm,
        title=f"{L('قسيمة مرتب')} {item.employee_code_snapshot}",
        author="M.Y. Business Hotel HR System",
    )
    available_width = A4[0] - document.leftMargin - document.rightMargin

    period = f"{run.period_start.strftime('%d/%m/%Y')} {L('إلى')} {run.period_end.strftime('%d/%m/%Y')}"
    story = [
        Paragraph(cell_(L("قسيمة مرتب")), title_style),
        Paragraph(cell_(f"{L('الفترة:')} {period} · {L('تاريخ الصرف:')} {run.pay_date.strftime('%d/%m/%Y')}"), meta_style),
        Spacer(1, 3 * mm),
    ]

    info_rows = [
        [L("كود الموظف"), item.employee_code_snapshot, L("الموظف"), item.employee_name_snapshot],
        [L("القسم"), item.department_name_snapshot, L("المسمى الوظيفي"), item.position_snapshot],
    ]
    info_table = LongTable(
        [[Paragraph(cell_(str(value)), cell_style) for value in row] for row in info_rows],
        colWidths=[available_width * 0.18, available_width * 0.32, available_width * 0.18, available_width * 0.32],
        hAlign="RIGHT",
    )
    info_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAB4BE")),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#FAF3E3")),
        ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#FAF3E3")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(info_table)

    def money_rows(pairs):
        return [[Paragraph(cell_(label), cell_style), Paragraph(cell_(_fmt_number(amount)), cell_style)] for label, amount in pairs]

    earnings = [(L("الأساسي"), item.base_salary)]
    for label, amount in [
        ("مكافأة حضور", item.attendance_bonus),
        ("مكافأة أداء", item.performance_bonus),
        ("مكافأة إشغال", item.occupancy_bonus),
        ("مكافآت أخرى", item.other_bonus),
        ("مكافآت الإجراءات", item.staff_reward_amount),
    ]:
        if not _zero(amount):
            earnings.append((L(label), amount))
    if not _zero(item.overtime_amount):
        earnings.append((f"{L('قيمة الإضافي')} ({_fmt_number(item.overtime_hours)} {L('ساعة')})", item.overtime_amount))
    gross = sum(float(amount) for _, amount in earnings)

    earn_rows = [[Paragraph(cell_(L("البند")), header_style), Paragraph(cell_(L("المبلغ")), header_style)]]
    earn_rows += money_rows(earnings)
    earn_rows.append([Paragraph(cell_(L("إجمالي المستحقات")), cell_style), Paragraph(cell_(_fmt_number(gross)), cell_style)])

    deductions = []
    if not _zero(item.absence_deduction):
        deductions.append((f"{L('خصم الغياب')} ({_fmt_number(item.absence_days)} {L('يوم')})", item.absence_deduction))
    for label, amount in [
        ("خصم تأخير/انصراف مبكر", item.lateness_deduction),
        ("حصة التأمين", item.insurance_share),
        ("الضرائب", item.taxes),
        ("خصومات أخرى", item.other_deductions),
        ("خصم السلف", item.advance_deduction),
        ("خصم الجزاءات", item.staff_sanction_amount),
    ]:
        if not _zero(amount):
            deductions.append((L(label), amount))
    total_deductions = sum(float(amount) for _, amount in deductions)

    ded_rows = [[Paragraph(cell_(L("البند")), header_style), Paragraph(cell_(L("المبلغ")), header_style)]]
    if deductions:
        ded_rows += money_rows(deductions)
    else:
        ded_rows.append([Paragraph(cell_(L("لا توجد خصومات")), cell_style), Paragraph(cell_(_fmt_number(0)), cell_style)])
    ded_rows.append([Paragraph(cell_(L("إجمالي الخصومات")), cell_style), Paragraph(cell_(_fmt_number(total_deductions)), cell_style)])

    for heading, rows in ((L("المستحقات"), earn_rows), (L("الخصومات"), ded_rows)):
        story.append(Paragraph(cell_(heading), section_style))
        table = LongTable(rows, colWidths=[available_width * 0.62, available_width * 0.38], hAlign="RIGHT")
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#24415E")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAB4BE")),
            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F3EDE0")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 2.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5),
        ]))
        story.append(table)

    paid, remaining, status = payment_totals
    status_label = {"unpaid": L("غير مدفوع"), "partially_paid": L("مدفوع جزئيًا")}.get(status, L("مدفوع بالكامل"))
    story.append(Paragraph(cell_(L("الصافي والصرف")), section_style))
    net_rows = [
        [Paragraph(cell_(L("صافي المستحق")), header_style), Paragraph(cell_(_fmt_number(item.net_pay)), header_style)],
        [Paragraph(cell_(L("المدفوع")), cell_style), Paragraph(cell_(_fmt_number(paid)), cell_style)],
        [Paragraph(cell_(L("المتبقي")), cell_style), Paragraph(cell_(_fmt_number(remaining)), cell_style)],
        [Paragraph(cell_(L("حالة الصرف")), cell_style), Paragraph(cell_(status_label), cell_style)],
    ]
    net_table = LongTable(net_rows, colWidths=[available_width * 0.62, available_width * 0.38], hAlign="RIGHT")
    net_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#24415E")),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#AAB4BE")),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    story.append(net_table)

    if item.note:
        story.append(Spacer(1, 2 * mm))
        story.append(Paragraph(cell_(f"{L('ملاحظات:')} {item.note}"), cell_style))

    story.append(Spacer(1, 12 * mm))
    signature_rows = [[
        Paragraph(cell_(L("توقيع الموظف: ........................")), meta_style),
        Paragraph(cell_(L("اعتماد المدير: ........................")), meta_style),
    ]]
    signature_table = LongTable(signature_rows, colWidths=[available_width / 2] * 2, hAlign="RIGHT")
    signature_table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "BOTTOM")]))
    story.append(signature_table)

    footer_text = f"M.Y. Business Hotel — {L('استُخرج في:')} {generated_at.strftime('%d/%m/%Y %H:%M')}"
    document.build(story, canvasmaker=_NumberedCanvas.build(font_name, footer_text, language))
    return output.getvalue()
