"""Smart scan: states, unavailable distinction, localized labels."""
import app.ai_smart_scan as scan


def test_states_and_unavailable_distinction():
    def counts(check):
        if check["key"] == "leave_drafts":
            raise RuntimeError("db down")
        return {"attendance_review": 3, "advance_pending": 0, "payroll_drafts": 0,
                "staff_action_drafts": 0, "attendance_months_review": 0,
                "import_issues": 0, "attendance_review_notes": 0}[check["key"]]

    results = {r["key"]: r for r in scan.build_smart_scan("en", counts)}
    assert results["attendance_review"]["status"] == "attention"
    assert results["attendance_review"]["count"] == 3
    assert results["advance_pending"]["status"] == "clear"
    assert results["leave_drafts"]["status"] == "unavailable"
    assert results["leave_drafts"]["count"] is None


def test_labels_localized():
    def zero(check):
        return 0
    en = {r["key"]: r for r in scan.build_smart_scan("en", zero)}
    zh = {r["key"]: r for r in scan.build_smart_scan("zh", zero)}
    ar = {r["key"]: r for r in scan.build_smart_scan("ar", zero)}
    assert en["payroll_drafts"]["label"] == "Unapproved payroll runs"
    assert zh["payroll_drafts"]["label"] == "未批准的工资批次"
    assert ar["payroll_drafts"]["label"] == "مسيرات رواتب غير معتمدة"
    assert en["staff_action_drafts"]["label"] == "Draft staff actions awaiting approval"
    assert zh["attendance_months_review"]["label"] == "待批准的考勤月份"
    assert ar["import_issues"]["label"] == "مشاكل استيراد حضور بانتظار الحل"
    assert en["attendance_review_notes"]["label"] == "Attendance records carrying review notes"
