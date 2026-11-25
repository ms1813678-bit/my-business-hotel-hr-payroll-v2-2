"""Characterization of is_command_request / wants_execution on a synthetic AR/EN/ZH corpus.

Corpus: invented phrases from a free DeepSeek web chat (2026-11-25); its intent labels are NOT
policy and were reviewed by hand. Baseline b4dd066; the matcher change (polite prefixes + correction verbs) is in the same candidate patch.
Routing only decides whether a request is sent to the approval gateway; the gateway still
needs explicit human confirmation, and wants_execution (the explicit-execute prefix) must be
False for every phrase here.
"""
import pytest

from app.ai_agent import is_command_request, wants_execution

READ_OR_NEGATED_NOT_ROUTED = [
    "هو أنا سجلت حضور النهارده؟",
    "عايز أعرف ميعاد الخروج بتاعي إمتى",
    "في غياب متسجل عليّا الشهر ده؟",
    "إيه ساعات الشغل بتاعتي الأسبوع اللي فات؟",
    "أنا اتأخرت كام دقيقة امبارح؟",
    "رصيد الإجازات المتبقي عندي كام؟",
    "متغيّرش تسجيل الحضور",
    "بلاش تعدّل حاجة النهارده",
    "مش عايز أضيف غياب",
    "متلمسش تسجيل الخروج",
    "مفيش داعي نمسح الحضور",
    "هل سجّلت حضوري اليوم؟",
    "أريد معرفة وقت تسجيل خروجي",
    "هل يوجد غياب مسجل عليّ هذا الشهر؟",
    "ما هي ساعات عملي الأسبوع الماضي؟",
    "كم دقيقة تأخرت أمس؟",
    "كم رصيد إجازاتي المتبقي؟",
    "لا تغيّر تسجيل الحضور",
    "لا تعدّل أي شيء اليوم",
    "لا أريد إضافة غياب",
    "لا تمسّ تسجيل الخروج",
    "لا حاجة لحذف الحضور",
    "Did I check in today?",
    "What time was my checkout yesterday?",
    "Is there an absence recorded for me this month?",
    "What were my work hours last week?",
    "How many minutes was I late yesterday?",
    "How much leave balance do I have left?",
    "Don't change the check-in record.",
    "Don't edit anything today.",
    "I don't want to add an absence.",
    "Don't touch the checkout record.",
    "No need to delete the attendance.",
    "我今天打卡了吗？",
    "我昨天几点签退的？",
    "这个月有我的缺勤记录吗？",
    "我上周的工作时间是怎样的？",
    "我昨天迟到了几分钟？",
    "我还剩多少假期余额？",
    "不要改打卡记录。",
    "今天什么都不要改。",
    "我不想添加缺勤记录。",
    "别动签退记录。",
    "不用删除打卡记录。",
]

CHANGE_REQUESTS_ROUTED = [
    "عدّل تسجيل الحضور بتاعي ليوم الاتنين",
    "غيّر ميعاد الحضور لـ ٩ الصبح",
    "سجّللي إجازة يوم الحد",
    "أضف غيابًا ليوم الخميس",
    "احذف تسجيل خروجي أمس",
    "غيّر وقت الحضور إلى التاسعة صباحًا",
    "سجّل لي إجازة يوم الأحد",
    "عدّل قيد التأخير ليوم الثلاثاء",
    "Add an absence for Thursday.",
    "Delete yesterday's checkout record.",
    "Change the check-in time to 9 a.m.",
    "Record a leave day for me on Sunday.",
]

CANCEL_REQUESTS_ROUTED = [
    "ألغي طلب تعديل الإجازة",
    "ألغِ طلب تعديل الإجازة",
    "Cancel the leave-change request.",
    "取消休假修改申请。",
]

EVERYDAY_CHANGE_REQUESTS_NEWLY_ROUTED = [
    "ضيف غياب يوم الخميس",
    "امسح تسجيل الخروج بتاع امبارح",
    "صحّح التأخير بتاع يوم التلات",
    "صحّح تسجيل حضوري يوم الاثنين",
    "Correct my check-in record for Monday.",
    "Fix the lateness entry for Tuesday.",
    "请更正我周一的打卡记录。",
    "请添加周四的缺勤记录。",
    "请删除昨天的签退记录。",
    "请把打卡时间改成上午九点。",
    "请给我登记周日的休假。",
    "请修正周二的迟到记录。",
]


ALL = READ_OR_NEGATED_NOT_ROUTED + CHANGE_REQUESTS_ROUTED + CANCEL_REQUESTS_ROUTED + EVERYDAY_CHANGE_REQUESTS_NEWLY_ROUTED


@pytest.mark.parametrize("phrase", READ_OR_NEGATED_NOT_ROUTED)
def test_queries_and_negations_never_reach_the_gateway(phrase):
    assert not is_command_request(phrase)


@pytest.mark.parametrize("phrase", CHANGE_REQUESTS_ROUTED + CANCEL_REQUESTS_ROUTED)
def test_change_and_cancel_requests_route_to_gateway_for_confirmation(phrase):
    # Cancelling a change request can itself be a mutation, so it is routed, not treated as read-only.
    assert is_command_request(phrase)


@pytest.mark.parametrize("phrase", ALL)
def test_no_phrase_here_is_an_explicit_execute_request(phrase):
    assert not wants_execution(phrase)


@pytest.mark.parametrize("phrase", EVERYDAY_CHANGE_REQUESTS_NEWLY_ROUTED)
def test_everyday_change_requests_with_politeness_or_correction_verbs_now_route(phrase):
    assert is_command_request(phrase)


POLITE_NEGATIONS_AND_QUERIES = [
    "please don't change the check-in record", "Please do not edit anything today", "kindly show me my attendance",
    "Please show how many employees are late", "How do I fix a late entry?", "请不要修改打卡记录", "请问今天有多少人迟到？",
    "麻烦您告诉我本月缺勤人数", "请假余额 MY-0007", "请查询员工MY-0001的假期", "من فضلك لا تعدّل حاجة النهارده",
    "لو سمحت كم عدد الموظفين الحاليين؟", "من فضلك اعرض عدد مرات التأخير للموظف MY-9001", "Correct answer: how many staff are active?",
    "把这个问题解释一下", "给我看本月考勤",
    # review follow-up: history/explanation requests that contain a change verb inside a noun or object
    "请把打卡修改记录显示出来", "请把员工 MY-0001 的修改历史列出来", "把考勤记录的更正历史告诉我", "请把上一个回答里的说明改得更简单一点",
    "Please correct the explanation you gave about late arrivals", "Please fix the wording of your last answer", "Could you fix the grammar of that summary?",
    "Please remove the extra text from your reply", "Show the correction history for MY-0001's check-in record",
    "من فضلك صحّح الشرح اللي كتبته عن التأخير", "لو سمحت امسح الجملة الزيادة من ردك", "ضيف توضيح لإجابتك السابقة", "من فضلك اعرض سجل تصحيح الحضور",
]


@pytest.mark.parametrize("phrase", POLITE_NEGATIONS_AND_QUERIES)
def test_polite_negations_and_queries_still_do_not_route(phrase):
    assert not is_command_request(phrase)


@pytest.mark.parametrize("phrase", ["Please add a manual attendance record for MY-0001", "من فضلك عدّل حضور MY-0001", "请删除员工 MY-0004 昨天的签退记录"])
def test_polite_commands_route_but_never_auto_execute(phrase):
    assert is_command_request(phrase)
    assert not wants_execution(phrase)


@pytest.mark.parametrize("prefix", ["نفذ ", "نفّذ ", "نفذ: ", "نفّذ: "])
def test_arabic_execute_prefix_spellings_route_and_mark_execution(prefix):
    assert is_command_request(prefix + "اعتمد مسير الرواتب رقم 1")
    assert wants_execution(prefix + "اعتمد مسير الرواتب رقم 1")


@pytest.mark.parametrize("phrase", ["احذف مسودة الإجازة رقم 12", "سجل حضور يدوي للموظف MY-0001"])
def test_arabic_bare_command_verbs_route_without_execution(phrase):
    assert is_command_request(phrase)
    assert not wants_execution(phrase)


@pytest.mark.parametrize("phrase", [
    "Please correct the lateness entry for MY-0001 on Tuesday", "Please fix my check-in record for Monday",
    "请把 MY-0001 周二的打卡时间改成九点", "麻烦您修正周二的迟到记录", "من فضلك صحّح تسجيل حضور MY-0001 يوم الاثنين", "لو سمحت امسح غياب يوم الخميس",
])
def test_everyday_record_changes_still_route_after_narrowing(phrase):
    assert is_command_request(phrase)
    assert not wants_execution(phrase)


WRITER_PROBES_SHOULD_ROUTE = [
    "Please correct my attendance record?", "Fix the attendance entry and show me the preview",
    "请把员工打卡时间改成九点，可以吗？", "Please correct employee MY001 attendance history", "صحح رصيد الموظف من فضلك؟",
]
WRITER_PROBES_SHOULD_NOT_ROUTE = [
    "请把修改记录显示出来", "把这个月的修改记录给我看", "Kindly correct my explanation of leave rules",
    "صحح لي شرح حساب الرصيد", "please remove the word add from this sentence",
]


@pytest.mark.parametrize("phrase", WRITER_PROBES_SHOULD_ROUTE)
def test_writer_probes_that_should_route(phrase):
    assert is_command_request(phrase)
    assert not wants_execution(phrase)


@pytest.mark.parametrize("phrase", WRITER_PROBES_SHOULD_NOT_ROUTE)
def test_writer_probes_that_should_not_route(phrase):
    assert not is_command_request(phrase)


@pytest.mark.parametrize('phrase', [
    'Please correct the leave explanation',
    'Fix the attendance explanation',
    'صحح شرح رصيد الإجازات',
    'صحح توضيح الرصيد في النص',
    '请修正考勤说明',
])
def test_text_objects_are_not_writes_regardless_of_hr_word_order(phrase):
    assert not is_command_request(phrase)
    assert not wants_execution(phrase)

@pytest.mark.parametrize('phrase', [
    'Fix the attendance entry and explain the change',
    'Please correct my attendance record?',
    '请把员工打卡时间改成九点，可以吗？',
])
def test_clear_hr_change_can_request_a_later_explanation(phrase):
    assert is_command_request(phrase)
    assert not wants_execution(phrase)
