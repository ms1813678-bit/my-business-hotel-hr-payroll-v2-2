"""Optional local-only AI assistant.

The assistant talks only to a model listening on localhost (Ollama-compatible API).
If no local model is available, the application remains fully usable and the UI
reports that the assistant is unavailable.
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse
from typing import Any
from urllib import error, request

from app.i18n import translate_text

OLLAMA_BASE_URL = os.getenv("LOCAL_AI_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("LOCAL_AI_MODEL", "qwen2.5:3b")

def _ensure_loopback() -> None:
    host = urlparse(OLLAMA_BASE_URL).hostname
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("الاتصال بالنموذج مسموح فقط على الجهاز المحلي.")

TIMEOUT_SECONDS = 1.5


def _post_json(path: str, payload: dict[str, Any], timeout: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    _ensure_loopback()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        f"{OLLAMA_BASE_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def local_model_status(language: str = "ar") -> dict[str, Any]:
    try:
        _ensure_loopback()
    except RuntimeError as exc:
        return {"available": False, "server": False, "model": OLLAMA_MODEL, "message": translate_text(str(exc), language) if str(exc) == "الاتصال بالنموذج مسموح فقط على الجهاز المحلي." else str(exc)}
    """Return availability without ever contacting a remote service."""
    req = request.Request(f"{OLLAMA_BASE_URL}/api/tags", method="GET")
    try:
        with request.urlopen(req, timeout=0.8) as response:
            data = json.loads(response.read().decode("utf-8"))
        names = {str(item.get("name", "")) for item in data.get("models", [])}
        installed = OLLAMA_MODEL in names or any(name.startswith(f"{OLLAMA_MODEL}:") for name in names)
        message = translate_text("المساعد المحلي جاهز." if installed else "خادم الذكاء المحلي يعمل، لكن النموذج المطلوب غير مثبت.", language)
        return {
            "available": installed,
            "server": True,
            "model": OLLAMA_MODEL,
            "message": message,
        }
    except (OSError, ValueError, error.URLError):
        return {
            "available": False,
            "server": False,
            "model": OLLAMA_MODEL,
            "message": translate_text("المساعد المحلي غير متاح حاليًا. النظام الأساسي يعمل بشكل طبيعي.", language),
        }


_SYSTEM_PROMPTS = {
    "ar": (
        "أنت مساعد موارد بشرية محلي لنظام M.Y. Business Hotel. "
        "أجب بالعربية الواضحة وباختصار. استخدم البيانات التي يرسلها النظام فقط. "
        "لا تخترع بيانات أو أرقامًا. لا تعتمد راتبًا أو إجازة أو حضورًا أو سلفة، "
        "ولا تنفذ أي تغيير؛ أنت مساعد تحليلي فقط. إذا كانت البيانات ناقصة فاذكر ذلك. "
        "عند اقتراح إجراء مؤثر، اعرضه كاقتراح يحتاج مراجعة وتأكيد مسؤول الموارد البشرية."
    ),
    "en": (
        "You are a local HR assistant for the M.Y. Business Hotel system. "
        "Answer in clear, concise English. Use only the data the system sends you. "
        "Never invent data or numbers. Never approve payroll, leave, attendance or "
        "advances, and never apply any change; you are an analytical assistant only. "
        "If data is missing, say so. When suggesting an impactful action, present it "
        "as a suggestion that needs HR administrator review and confirmation."
    ),
    "zh": (
        "你是 M.Y. Business Hotel 系统的本地人力资源助手。"
        "请用简明的中文回答。只能使用系统发送给你的数据。"
        "不要编造任何数据或数字。不要批准工资、休假、考勤或预支款，"
        "也不要执行任何更改；你只是分析助手。数据不足时请明确说明。"
        "提出有影响的操作时，以需要人力资源管理员审核和确认的建议形式呈现。"
    ),
}


def build_system_prompt(language: str = "ar") -> str:
    return _SYSTEM_PROMPTS.get(language, _SYSTEM_PROMPTS["ar"])


def ask_local_model(question: str, context: dict[str, Any], language: str = "ar") -> str:
    """Ask the configured local model; raises RuntimeError when unavailable."""
    from app.security_roster_compare import (
        answer_security_roster_comparison,
        is_security_comparison_request,
    )
    from app.ai_hr_queries import answer_structured_hr_query, is_structured_hr_query
    if is_security_comparison_request(question):
        # Deterministic review report; does not mutate attendance or payroll.
        return answer_security_roster_comparison(question, language=language)
    if is_structured_hr_query(question):
        return answer_structured_hr_query(question, language=language)
    status = local_model_status(language)
    if not status["available"]:
        raise RuntimeError(status["message"])
    response = _post_json(
        "/api/chat",
        {
            "model": OLLAMA_MODEL,
            "stream": False,
            "messages": [
                {"role": "system", "content": build_system_prompt(language)},
                {
                    "role": "user",
                    "content": (
                        translate_text("سؤال المسؤول", language) + f": {question.strip()}\n\n"
                        + translate_text("بيانات النظام الحالية (للقراءة فقط)", language)
                        + f": {json.dumps(context, ensure_ascii=False)}"
                    ),
                },
            ],
        },
        timeout=30,
    )
    answer = str(response.get("message", {}).get("content", "")).strip()
    if not answer:
        raise RuntimeError("لم يُرجع النموذج المحلي إجابة.")
    return answer


def local_fallback_answer(question: str, context: dict[str, Any], language: str = "ar") -> str:
    """Provide useful deterministic help even when no model is installed."""
    from app.security_roster_compare import (
        answer_security_roster_comparison,
        is_security_comparison_request,
    )
    from app.ai_hr_queries import answer_structured_hr_query, is_structured_hr_query
    if is_security_comparison_request(question):
        return answer_security_roster_comparison(question, language=language)
    if is_structured_hr_query(question):
        return answer_structured_hr_query(question, language=language)
    q = " ".join(question.casefold().split())
    metrics = context.get("metrics", {})
    # A count question about a past or specific date is unsupported: answer
    # with explicit uncertainty instead of today's numbers.
    import re as _re
    count_like = any(term in q for term in (
        "كام موظف", "كم موظف", "عدد الموظفين", "كام واحد", "كم عدد",
        "how many employees", "how many staff", "number of employees",
        "多少员工", "几位员工", "员工数量", "在职员工",
    ))
    dated = bool(_re.search(r"(19|20)\d{2}", q)) or any(term in q for term in (
        "امبارح", "أمس", "امس", "yesterday", "昨天", "昨日",
        "بكرة", "بكره", "غدا", "غدًا", "tomorrow", "明天",
        "last year", "السنة الماضية", "السنة اللي فاتت", "去年",
        "last month", "الشهر الماضي", "الشهر اللي فات", "上个月", "上月",
        "last week", "الأسبوع الماضي", "الاسبوع الماضي", "上周",
        "بتاريخ", "في تاريخ",
    ))
    if count_like and dated:
        unsupported = {
            "ar": "لا أستطيع تحديد عدد الموظفين في تاريخ أو فترة سابقة من السجلات الحالية؛ الأعداد المتاحة لدي هي الحالية فقط. يمكنك سؤالي عن العدد الحالي أو عن حضور يوم محدد.",
            "en": "I cannot determine the employee count for a past or specific date from the current records; only the current count is available to me. You can ask about the current count or about a specific day's attendance.",
            "zh": "我无法根据现有记录确定过去或指定日期的员工人数；我只能提供当前人数。您可以询问当前人数或某一天的考勤情况。",
        }
        return unsupported.get(language, unsupported["ar"])
    active_terms = ("عدد الموظفين", "الموظفين النشطين", "الموظفون النشطون", "active employees", "how many employees", "在职员工", "员工数量")
    if any(term in q for term in active_terms):
        return translate_text("عدد الموظفين النشطين حاليًا", language) + f": {metrics.get('active_employees', 0)}."
    if (("الحضور" in q or "attendance" in q or "考勤" in q) and ("مراجعة" in q or "مشكلة" in q or "review" in q or "issue" in q or "审核" in q or "问题" in q)):
        return translate_text("عدد سجلات الحضور التي تحتاج مراجعة حاليًا", language) + f": {metrics.get('attendance_review', 0)}."
    if (("الرواتب" in q or "payroll" in q or "工资" in q) and ("مفتوح" in q or "مسير" in q or "open" in q or "draft" in q or "打开" in q or "草稿" in q)):
        return translate_text("عدد مسيرات الرواتب المفتوحة أو المعاد فتحها", language) + f": {metrics.get('payroll_drafts', 0)}."
    return translate_text(
        "المساعد الذكي المحلي غير متاح حاليًا لأن النموذج المحلي غير مثبت أو غير مشغّل. "
        "يمكنك استخدام النظام كاملًا بشكل طبيعي. بعد تشغيل النموذج المحلي سأتمكن من تحليل السؤال "
        "باستخدام بيانات النظام المعروضة للمساعد، دون حفظ أي تغيير تلقائي.",
        language,
    )


def ask_local_model_json(system_prompt: str, user_prompt: str) -> dict[str, Any]:
    status = local_model_status()
    if not status["available"]:
        raise RuntimeError(status["message"])
    response = _post_json(
        "/api/chat",
        {"model": OLLAMA_MODEL, "stream": False, "format": "json",
         "messages": [{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_prompt}]},
        timeout=30,
    )
    raw = str(response.get("message", {}).get("content", "")).strip()
    if not raw:
        raise RuntimeError("لم يُرجع النموذج المحلي خطة أمر.")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("النموذج المحلي لم يُرجع JSON صالحًا.") from exc
