"""In-app usage guide: curated how-to answers with the real page links."""
from app.usage_guide import (
    _TOPICS, format_usage_guide, is_usage_guide_request,
)


def test_matches_how_to_questions_in_three_languages():
    assert is_usage_guide_request("أصحح حضور موظف إزاي؟") is not None
    assert is_usage_guide_request("How do I correct attendance?") is not None
    assert is_usage_guide_request("怎么补打卡？") is not None
    assert is_usage_guide_request("كيف أضيف موظف جديد؟")["path"] == "/employees"
    assert is_usage_guide_request("How to create a payroll run?")["path"] == "/payroll"
    assert is_usage_guide_request("如何申请请假？")["path"] == "/leaves"


def test_data_questions_do_not_match_the_guide():
    assert is_usage_guide_request("كم عدد الموظفين النشطين؟") is None
    assert is_usage_guide_request("ملخص الأسبوع") is None
    assert is_usage_guide_request("Weekly summary") is None
    assert is_usage_guide_request("سلف الموظفين النشطة") is None
    assert is_usage_guide_request("") is None


def test_every_topic_is_trilingual_with_a_real_path():
    for topic in _TOPICS:
        assert topic["path"].startswith("/")
        for lang in ("ar", "en", "zh"):
            assert topic[lang].strip(), (topic["path"], lang)
        assert len({topic["ar"], topic["en"], topic["zh"]}) == 3  # not copied text


def test_format_includes_steps_and_page_link():
    topic = is_usage_guide_request("أصحح حضور موظف إزاي؟")
    ar = format_usage_guide(topic, "ar")
    en = format_usage_guide(topic, "en")
    zh = format_usage_guide(topic, "zh")
    assert "فتح الصفحة: /attendance" in ar
    assert "Open the page: /attendance" in en
    assert "打开页面: /attendance" in zh


def test_dispatch_through_structured_query():
    from app.ai_hr_queries import is_structured_hr_query, answer_structured_hr_query

    assert is_structured_hr_query("أصحح حضور موظف إزاي؟")
    answer = answer_structured_hr_query("أصحح حضور موظف إزاي؟", language="ar")
    assert "/attendance" in answer
    answer_en = answer_structured_hr_query("How do I correct attendance?", language="en")
    assert "Manual attendance" in answer_en and "/attendance" in answer_en
