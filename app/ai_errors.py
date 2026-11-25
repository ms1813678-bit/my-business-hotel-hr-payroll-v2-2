"""Typed, already-localized AI refusals backed by a finite authored catalog.

A marker class alone proves nothing (anyone can raise it with any text), so a route renders one of these only
when the exception is exactly one of the two classes below, carries a single str argument, and that text is
byte-for-byte one of the AUTHORED messages for the request language. Everything else is untrusted text.
Both classes keep their built-in base, so existing ``except ValueError`` / ``except RuntimeError`` still match.
tests/test_ai_error_display.py checks that every raise site's text is in this catalog.
"""

# (ar, en, zh) messages the approval gateway, plan validation, JSON planning and the review-required route raise.
AUTHORED = (
    ("انتهت صلاحية طلب الموافقة.",
     "The approval request has expired.",
     "批准请求已过期。"),
    ("طلب الموافقة غير موجود.",
     "Approval request not found.",
     "未找到该批准请求。"),
    ("هذا الطلب لم يعد قابلًا للرفض.",
     "This request can no longer be rejected.",
     "该请求已无法拒绝。"),
    ("يلزم تأكيد صريح من مسؤول الموارد البشرية.",
     "Explicit human HR confirmation required.",
     "需要人力资源人员明确确认。"),
    ("هذا الطلب سبق التعامل معه ولا يمكن تنفيذه مرة أخرى.",
     "This request was already handled and cannot be executed again.",
     "该请求已处理，无法再次执行。"),
    ("خطة التنفيذ لم تعد مطابقة للخطة التي تم عرضها.",
     "The stored plan no longer matches the one that was shown.",
     "存储的执行计划与已展示的计划不一致。"),
    ("تغيرت بيانات السجل منذ عرض المعاينة. جهّز الأمر مرة أخرى وراجع القيم الجديدة.",
     "The record changed since the preview was shown. Prepare the command again and review the new values.",
     "自预览以来记录已变更。请重新准备指令并核对新值。"),
    ("يلزم تأكيدك الشخصي لمراجعة تغطية الحضور.",
     "Your explicit attendance coverage confirmation is required.",
     "需要您明确确认已审核出勤覆盖情况。"),
    ("طلب الموافقة غير متاح أو انتهت صلاحيته.",
     "Approval is unavailable or expired.",
     "批准请求不可用或已过期。"),
    ("تغيرت البيانات. جهز معاينة جديدة.",
     "Data changed. Prepare a new preview.",
     "数据已变更。请重新生成预览。"),
    ("النموذج المحلي لم يُرجع أمرًا بصيغة JSON صحيحة.",
     "The local model did not return a valid JSON command.",
     "本地模型未返回有效的 JSON 指令。"),
    ("الأمر المطلوب غير مسموح به في المساعد.",
     "The requested command is not allowed in the assistant.",
     "助手不允许该请求指令。"),
    ("لم يستجب النموذج المحلي في الوقت المناسب.",
     "The local model did not respond in time.",
     "本地模型未及时响应。"),
    ("بيانات الأمر غير صالحة. يلزم إدخال قيم صحيحة وصريحة قبل المراجعة.",
     "Invalid command values. Supply valid explicit values before review.",
     "指令数据无效。请在审核前提供有效且明确的值。"),
    ("راجع التفاصيل قبل التأكيد.",
     "Review the details before confirming.",
     "确认前请先查看详情。"),
)

_LANGS = ("ar", "en", "zh")
AUTHORED_BY_LANGUAGE = {lang: frozenset(item[i] for item in AUTHORED) for i, lang in enumerate(_LANGS)}


def is_authored(message: object, language: object) -> bool:
    return type(language) is str and type(message) is str and message in AUTHORED_BY_LANGUAGE.get(language, frozenset())


class LocalizedMessage:
    language = "ar"


class LocalizedValueError(LocalizedMessage, ValueError):
    def __init__(self, message: str, language: str = "ar"):
        super().__init__(message)
        self.language = language


class LocalizedRuntimeError(LocalizedMessage, RuntimeError):
    def __init__(self, message: str, language: str = "ar"):
        super().__init__(message)
        self.language = language
