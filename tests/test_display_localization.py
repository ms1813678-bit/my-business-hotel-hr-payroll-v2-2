"""Items 47/48/50: upload picker labels, review-status and department display
localization, and visible day/month/year date examples."""
from pathlib import Path

from app.audit_display import STATUS_LABELS_AR
from app.i18n import translate_html, translate_text


def test_ready_for_review_and_under_review_have_arabic_labels():
    assert STATUS_LABELS_AR["ready_for_review"] == "جاهزة للاعتماد"
    assert STATUS_LABELS_AR["under_review"] == "قيد المراجعة"


def test_review_status_labels_localize_to_en_and_zh():
    assert translate_text("جاهزة للاعتماد", "en") == "Ready for Approval"
    assert translate_text("جاهزة للاعتماد", "zh") == "待批准"
    assert translate_text("قيد المراجعة", "en") == "Under Review"
    assert translate_text("قيد المراجعة", "zh") == "审核中"


def test_upload_picker_labels_localize():
    rendered = translate_html(
        '<html lang="ar"><body><label class="file-picker-btn">اختر الملف</label>'
        '<span class="file-picker-name">لم يُختر ملف بعد</span></body></html>',
        "en",
    )
    assert "Choose File" in rendered
    assert "No file chosen yet" in rendered


def test_known_department_names_localize_display_only():
    assert translate_text("الاستقبال", "en") == "Reception"
    assert translate_text("الأمن", "en") == "Security"
    assert translate_text("التدبير المنزلي", "en") == "Housekeeping"
    assert translate_text("الاستقبال", "zh") == "前台"
    # Unknown custom names fall back to the stored name, never mutated.
    assert translate_text("قسم مخصص جديد", "en") == "قسم مخصص جديد"


def test_file_picker_widget_present_on_upload_pages():
    base = Path("app/templates/base.html").read_text(encoding="utf-8")
    assert "/static/file-picker.js" in base
    for page in ("attendance_import.html", "security_roster.html", "employees.html"):
        text = Path("app/templates", page).read_text(encoding="utf-8")
        assert "file-picker-input" in text and "اختر الملف" in text


def test_date_picker_shows_day_month_year_example_placeholder():
    js = Path("app/static/date-picker.js").read_text(encoding="utf-8")
    assert "25/10/2026" in js
    assert "DD/MM/YYYY" in js
    assert "PLACEHOLDER_TEXT" in js
