"""Display-side localization of stored attendance review_notes tokens."""
from app.main import _attendance_notes_display


def test_known_codes_localized_per_language():
    value = "missing_fingerprint_evidence; dst_wall_clock_duration_uncertain"
    ar = _attendance_notes_display(value, "ar")
    en = _attendance_notes_display(value, "en")
    zh = _attendance_notes_display(value, "zh")
    assert "dst_wall_clock" not in ar and "missing_fingerprint" not in ar
    assert en == "No fingerprint evidence for this day; Duration uncertain because of the daylight-saving change hour"
    assert zh == "当天没有指纹打卡记录; 因夏令时切换小时，时长无法确定"
    assert ar == "لا توجد بصمة مثبتة لهذا اليوم; المدة غير مؤكدة بسبب ساعة تغيير التوقيت الصيفي"


def test_system_sentence_and_hr_decision_prefix():
    value = "Manual HR attendance entered and awaiting approval.; HR decision: سبب مكتوب بيد المستخدم"
    en = _attendance_notes_display(value, "en")
    assert en == "Manual HR attendance entered and awaiting approval.; HR decision: سبب مكتوب بيد المستخدم"
    ar = _attendance_notes_display(value, "ar")
    assert ar.startswith("إدخال حضور يدوي من الموارد البشرية في انتظار الاعتماد.; قرار الموارد البشرية: ")
    assert ar.endswith("سبب مكتوب بيد المستخدم")


def test_unknown_tokens_and_user_text_pass_through():
    value = "future_unknown_code; ملاحظة حرة من المستخدم"
    assert _attendance_notes_display(value, "en") == value
    assert _attendance_notes_display(value, "zh") == value
    assert _attendance_notes_display(None, "en") is None
