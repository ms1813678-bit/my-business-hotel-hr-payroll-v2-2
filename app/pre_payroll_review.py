"""Deterministic pre-payroll review (read-only).

Approved scope: before a payroll run is approved, point the HR administrator
at (a) attendance that is not settled for the month and (b) deductions that
look unusual against the employee's own payroll history, each with the
supporting records. These are review suggestions, never verdicts: the report
changes nothing, approves nothing, and an employee without enough history is
reported as "insufficient data", never as "no issues".

Deterministic criteria (no model guessing):
- Unsettled attendance: a run employee has attendance daily records in the
  month whose status is needs_review or ready_for_review.
- Missing attendance: a run employee has an explicit per-date security shift
  assignment in the month but no attendance daily record at all for that
  date (a date covered by a confirmed leave record is excused). Missing is
  reported separately from unsettled: absence of a row is not the same as a
  settled day. Only explicitly assigned days can be checked this way; run
  employees without any per-date assignment in the month are listed as
  coverage-unproven (insufficient data), never as settled.
- Unusual deduction: the item's reviewable deductions (absence + lateness +
  staff sanctions + other deductions; scheduled advance installments are
  expected repayments and excluded) exceed the employee's average over their
  previous payroll items (up to 3 earlier runs) by the configured ratio and
  absolute floor. The defaults (2x and 100 EGP) are REVIEW HEURISTICS chosen
  to surface outliers for a human look, not hotel HR policy and not proof of
  error; both are parameters so management can tune them. An unflagged item
  is "not unusual under this heuristic", never "correct". With no previous
  items the baseline is reported as insufficient data.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.db import SessionLocal
from app.models import (
    AttendanceDailyRecord,
    LeaveRecord,
    PayrollItem,
    PayrollRun,
    SecurityShiftAssignment,
)

_UNSETTLED_ATTENDANCE = ("needs_review", "ready_for_review")
_BASELINE_RUNS = 3
_RATIO_THRESHOLD = Decimal("2")
_ABS_FLOOR = Decimal("100")


def _month_bounds(month: date) -> tuple[datetime, datetime]:
    start = datetime(month.year, month.month, 1)
    if month.month == 12:
        end = datetime(month.year + 1, 1, 1)
    else:
        end = datetime(month.year, month.month + 1, 1)
    return start, end


def _reviewable_deductions(item: PayrollItem) -> Decimal:
    return (
        Decimal(item.absence_deduction)
        + Decimal(item.lateness_deduction)
        + Decimal(item.staff_sanction_amount)
        + Decimal(item.other_deductions)
    )


def build_pre_payroll_review(
    month: date,
    *,
    ratio_threshold: Decimal = _RATIO_THRESHOLD,
    abs_floor: Decimal = _ABS_FLOOR,
) -> dict[str, Any]:
    with SessionLocal() as db:
        run = db.scalar(
            select(PayrollRun).where(PayrollRun.period_start == month).order_by(PayrollRun.id.desc())
        )
        report: dict[str, Any] = {
            "month": month.isoformat(),
            "run": None,
            "unsettled_attendance": [],
            "missing_attendance": [],
            "coverage_unproven": [],
            "unusual_deductions": [],
            "insufficient_baseline": [],
            "heuristics": {
                "ratio_threshold": str(ratio_threshold),
                "abs_floor": str(abs_floor),
                "baseline_runs": _BASELINE_RUNS,
            },
            "note": "note_readonly_pre_payroll",
        }
        if run is None:
            report["run"] = {"status": "not_created"}
            return report
        report["run"] = {"id": run.id, "status": run.status}

        items = db.scalars(
            select(PayrollItem).where(PayrollItem.payroll_run_id == run.id)
        ).all()
        start, end = _month_bounds(month)
        for item in items:
            unsettled = db.scalars(
                select(AttendanceDailyRecord)
                .where(
                    AttendanceDailyRecord.employee_id == item.employee_id,
                    AttendanceDailyRecord.work_date >= start.date(),
                    AttendanceDailyRecord.work_date < end.date(),
                    AttendanceDailyRecord.status.in_(_UNSETTLED_ATTENDANCE),
                )
                .order_by(AttendanceDailyRecord.work_date)
            ).all()
            if unsettled:
                report["unsettled_attendance"].append({
                    "employee_code": item.employee_code_snapshot,
                    "employee_name": item.employee_name_snapshot,
                    "records": [
                        {"id": record.id, "work_date": record.work_date.isoformat(), "status": record.status}
                        for record in unsettled
                    ],
                })

            # Missing attendance: explicitly assigned days with no record at
            # all. Only per-date security assignments prove an expectation,
            # so only they can prove a gap; confirmed leave excuses the day.
            assignments = db.scalars(
                select(SecurityShiftAssignment)
                .where(
                    SecurityShiftAssignment.employee_id == item.employee_id,
                    SecurityShiftAssignment.work_date >= start.date(),
                    SecurityShiftAssignment.work_date < end.date(),
                )
                .order_by(SecurityShiftAssignment.work_date)
            ).all()
            if not assignments:
                report["coverage_unproven"].append({
                    "employee_code": item.employee_code_snapshot,
                    "employee_name": item.employee_name_snapshot,
                })
            else:
                missing = []
                for assignment in assignments:
                    day = assignment.work_date
                    has_record = db.scalar(
                        select(AttendanceDailyRecord.id).where(
                            AttendanceDailyRecord.employee_id == item.employee_id,
                            AttendanceDailyRecord.work_date == day,
                        )
                    )
                    if has_record is not None:
                        continue
                    on_leave = db.scalar(
                        select(LeaveRecord.id).where(
                            LeaveRecord.employee_id == item.employee_id,
                            LeaveRecord.status == "confirmed",
                            LeaveRecord.start_date <= day,
                            LeaveRecord.end_date >= day,
                        )
                    )
                    if on_leave is not None:
                        continue
                    missing.append({"work_date": day.isoformat(), "assignment_id": assignment.id})
                if missing:
                    report["missing_attendance"].append({
                        "employee_code": item.employee_code_snapshot,
                        "employee_name": item.employee_name_snapshot,
                        "days": missing,
                    })

            current = _reviewable_deductions(item)
            previous_runs = db.scalars(
                select(PayrollRun)
                .where(PayrollRun.period_start < month, PayrollRun.id != run.id)
                .order_by(PayrollRun.period_start.desc())
                .limit(_BASELINE_RUNS)
            ).all()
            previous_items = []
            for previous_run in previous_runs:
                previous_item = db.scalar(
                    select(PayrollItem).where(
                        PayrollItem.payroll_run_id == previous_run.id,
                        PayrollItem.employee_id == item.employee_id,
                    )
                )
                if previous_item is not None:
                    previous_items.append((previous_run.period_start, previous_item))
            if not previous_items:
                if current > 0:
                    report["insufficient_baseline"].append({
                        "employee_code": item.employee_code_snapshot,
                        "employee_name": item.employee_name_snapshot,
                        "current_deductions": str(current),
                        "item_id": item.id,
                    })
                continue
            baseline = sum(_reviewable_deductions(prev) for _, prev in previous_items) / len(previous_items)
            if (
                current > 0
                and current >= baseline * ratio_threshold
                and current - baseline >= abs_floor
            ):
                report["unusual_deductions"].append({
                    "employee_code": item.employee_code_snapshot,
                    "employee_name": item.employee_name_snapshot,
                    "current_deductions": str(current),
                    "baseline_average": str(baseline.quantize(Decimal("0.01"))),
                    "baseline_months": [m.isoformat() for m, _ in previous_items],
                    "item_id": item.id,
                })
        return report


def _msg(language: str, ar: str, en: str, zh: str) -> str:
    return {"ar": ar, "en": en, "zh": zh}.get(language, ar)


def format_pre_payroll_review(report: dict[str, Any], language: str = "ar") -> str:
    month = report["month"]
    heuristics = report.get("heuristics") or {}
    ratio = heuristics.get("ratio_threshold", "2")
    floor = heuristics.get("abs_floor", "100")
    lines = [
        _msg(language,
             f"مراجعة ما قبل اعتماد رواتب {month} — اقتراحات مراجعة فقط، لا تغيّر ولا تعتمد شيئًا.",
             f"Pre-payroll review for {month} — review suggestions only; nothing is changed or approved.",
             f"{month} 工资批准前检查 — 仅供参考建议，不会更改或批准任何内容。"),
        _msg(language,
             f"معايير الفحص المعلنة (إرشادات مراجعة قابلة للضبط، وليست سياسة موارد بشرية معتمدة): خصم يُعتبر غير معتاد إذا بلغ {ratio}× متوسط الموظف وفاقه بما لا يقل عن {floor} جنيه، مقارنة بآخر {heuristics.get('baseline_runs', 3)} مسيرات سابقة كحد أقصى.",
             f"Stated check criteria (adjustable review heuristics, not adopted HR policy): a deduction is treated as unusual when it reaches {ratio}x the employee's average and exceeds it by at least {floor} EGP, compared against up to the last {heuristics.get('baseline_runs', 3)} runs.",
             f"公示检查标准（可调整的审查参考值，并非既定人事政策）：扣款达到员工平均值的 {ratio} 倍且至少高出 {floor} 埃镑时视为异常，最多对比最近 {heuristics.get('baseline_runs', 3)} 个批次。"),
    ]
    run = report.get("run") or {}
    if run.get("status") == "not_created":
        lines.append(_msg(language,
            "لا يوجد مسير رواتب لهذا الشهر بعد؛ أنشئ المسير أولًا ثم أعد المراجعة قبل الاعتماد.",
            "No payroll run exists for this month yet; create the run first, then re-run this review before approving.",
            "该月份尚未创建工资批次；请先创建批次，然后在批准前重新检查。"))
        return "\n".join(lines)
    lines.append(_msg(language,
        f"حالة المسير: {run.get('status')}.",
        f"Run status: {run.get('status')}.",
        f"批次状态：{run.get('status')}。"))

    unsettled = report["unsettled_attendance"]
    if unsettled:
        lines.append(_msg(language,
            f"حضور غير محسوم يحتاج تسوية قبل الاعتماد ({len(unsettled)} موظف):",
            f"Unsettled attendance to resolve before approval ({len(unsettled)} employees):",
            f"批准前需要处理的未结考勤（{len(unsettled)} 名员工）："))
        for entry in unsettled:
            days = ", ".join(f"{r['work_date']} (#{r['id']}, {r['status']})" for r in entry["records"])
            lines.append(f"- {entry['employee_code']} — {entry['employee_name']}: {days}")
    else:
        lines.append(_msg(language,
            "لا يوجد حضور غير محسوم لموظفي هذا المسير.",
            "No unsettled attendance for the employees in this run.",
            "该批次员工没有未结考勤。"))

    missing_att = report["missing_attendance"]
    if missing_att:
        lines.append(_msg(language,
            f"أيام مكلَّف بها بدون أي سجل حضور ({len(missing_att)} موظف) — غياب السجل لا يعني حضورًا مسوّى:",
            f"Assigned days with no attendance record at all ({len(missing_att)} employees) — a missing record is not a settled day:",
            f"已排班但完全无考勤记录的日期（{len(missing_att)} 名员工）— 缺少记录不等于考勤已结清："))
        for entry in missing_att:
            days = ", ".join(f"{d['work_date']} (#{d['assignment_id']})" for d in entry["days"])
            lines.append(f"- {entry['employee_code']} — {entry['employee_name']}: {days}")
    else:
        lines.append(_msg(language,
            "لا توجد أيام مكلَّف بها بدون سجل حضور ضمن الأيام التي يمكن إثبات وجوبها.",
            "No assigned days without an attendance record among the days that can be proven required.",
            "在可证明应出勤的日期中，没有缺考勤记录的已排班日期。"))

    unproven = report["coverage_unproven"]
    if unproven:
        lines.append(_msg(language,
            f"تغطية الحضور غير مثبتة ({len(unproven)} موظف): لا توجد تكليفات ورديات مؤرخة لهذا الشهر، فلا يمكن إثبات اكتمال سجلات الحضور — بيانات غير كافية، وليست تأكيدًا بالانتظام:",
            f"Attendance coverage not proven ({len(unproven)} employees): no dated shift assignments exist for this month, so complete attendance records cannot be proven — insufficient data, not a confirmation of regularity:",
            f"考勤覆盖无法证明（{len(unproven)} 名员工）：本月没有带日期的排班记录，无法证明考勤记录完整 — 数据不足，并不代表出勤正常："))
        for entry in unproven:
            lines.append(f"- {entry['employee_code']} — {entry['employee_name']}")

    unusual = report["unusual_deductions"]
    if unusual:
        lines.append(_msg(language,
            f"خصومات غير معتادة مقارنة بتاريخ الموظف ({len(unusual)}):",
            f"Deductions unusual against the employee's own history ({len(unusual)}):",
            f"与员工本人历史相比异常扣款（{len(unusual)}）："))
        for entry in unusual:
            lines.append(_msg(language,
                f"- {entry['employee_code']} — {entry['employee_name']}: {entry['current_deductions']} جنيه مقابل متوسط سابق {entry['baseline_average']} (أشهر المقارنة: {', '.join(entry['baseline_months'])}) — راجع بند الرواتب #{entry['item_id']}.",
                f"- {entry['employee_code']} — {entry['employee_name']}: {entry['current_deductions']} EGP vs previous average {entry['baseline_average']} (baseline months: {', '.join(entry['baseline_months'])}) — review payroll item #{entry['item_id']}.",
                f"- {entry['employee_code']} — {entry['employee_name']}：{entry['current_deductions']} 埃镑，此前平均 {entry['baseline_average']}（对比月份：{', '.join(entry['baseline_months'])}）— 请查看工资条目 #{entry['item_id']}。"))
    else:
        lines.append(_msg(language,
            "لم يُرصد خصم غير معتاد وفق المعيار الإرشادي المعلن أعلاه — هذا لا يعني أن الخصومات صحيحة، فالمعيار يلتقط الشذوذ الإحصائي فقط.",
            "No deduction stood out under the stated heuristic above — this does not mean the deductions are correct; the heuristic only catches statistical outliers.",
            "按照上述参考标准未发现突出扣款 — 这不代表扣款正确，该标准仅能捕捉统计异常。"))

    insufficient = report["insufficient_baseline"]
    if insufficient:
        lines.append(_msg(language,
            f"بيانات تاريخية غير كافية للمقارنة ({len(insufficient)}) — هذا ليس تأكيدًا بعدم وجود مشاكل:",
            f"Insufficient history to compare ({len(insufficient)}) — this is not a confirmation that there are no issues:",
            f"历史数据不足，无法比较（{len(insufficient)}）— 这不代表没有问题："))
        for entry in insufficient:
            lines.append(f"- {entry['employee_code']} — {entry['employee_name']}: {entry['current_deductions']} (#{entry['item_id']})")

    lines.append(_msg(language,
        "المصدر: مسير الرواتب وبنوده وسجلات الحضور اليومية المحلية. القرار النهائي لمسؤول الموارد البشرية.",
        "Source: the local payroll run, its items, and daily attendance records. The final decision belongs to the HR administrator.",
        "来源：本地工资批次、工资条目和每日考勤记录。最终决定权在人力资源管理员。"))
    return "\n".join(lines)
