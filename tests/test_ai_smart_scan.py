"""Smart scan: states, unavailable distinction, localized labels."""
import app.ai_smart_scan as scan


def test_states_and_unavailable_distinction():
    def counts(check):
        if check["key"] == "leave_drafts":
            raise RuntimeError("db down")
        return {"attendance_review": 3, "advance_pending": 0, "payroll_drafts": 0}[check["key"]]

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
