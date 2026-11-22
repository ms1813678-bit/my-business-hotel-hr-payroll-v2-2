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
    assert is_usage_guide_request("如何申请请假？")["path"] == "/leave"


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
    assert "فتح الصفحة: /attendance/review" in ar
    assert "Open the page: /attendance/review" in en
    assert "打开页面: /attendance/review" in zh


def test_dispatch_through_structured_query():
    from app.ai_hr_queries import is_structured_hr_query, answer_structured_hr_query

    assert is_structured_hr_query("أصحح حضور موظف إزاي؟")
    answer = answer_structured_hr_query("أصحح حضور موظف إزاي؟", language="ar")
    assert "/attendance/review" in answer
    answer_en = answer_structured_hr_query("How do I correct attendance?", language="en")
    assert "Manual attendance" in answer_en and "/attendance/review" in answer_en


def test_every_guide_path_resolves_to_a_real_route():
    from app.main import app
    get_paths = {
        route.path for route in app.routes
        if getattr(route, "methods", None) and "GET" in route.methods
    }
    for topic in _TOPICS:
        assert topic["path"] in get_paths, topic["path"]


def test_answer_links_filter_extracts_in_app_paths():
    from app.main import _answer_links
    links = _answer_links("خطوة أولى\nفتح السجلات: /leave\nنص عادي بلا رابط")
    assert links == [("فتح السجلات", "/leave")]
    assert _answer_links("") == []
    assert _answer_links(None) == []


def test_answer_links_rejects_non_in_app_or_hostile_paths():
    from app.main import _answer_links, _answer_text_without_links
    hostile = [
        "ت: //evil.com/phish",          # protocol-relative external URL
        "ت: /\\evil.com",               # backslash network-path variant
        "ت: /%2e%2e/secret",            # encoded traversal
        "ت: /leave/../../etc",          # dot segments
        "ت: javascript:alert(1)",       # no leading slash at all
        "ت: /definitely-not-a-route",   # not a real GET route
        "ت: https://evil.com/leave",    # absolute external URL
    ]
    for line in hostile:
        assert _answer_links(line) == [], line
        assert line in _answer_text_without_links(line)  # stays plain text
    # real routes survive; link lines are stripped from the text copy
    text = "نص أول\nفتح السجلات: /leave\nنص أخير"
    assert _answer_links(text) == [("فتح السجلات", "/leave")]
    stripped = _answer_text_without_links(text)
    assert "فتح السجلات: /leave" not in stripped
    assert "نص أول" in stripped and "نص أخير" in stripped
