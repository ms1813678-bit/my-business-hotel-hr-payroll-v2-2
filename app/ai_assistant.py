"""Optional local-only AI assistant.

The assistant talks only to a model listening on localhost (Ollama-compatible API).
If no local model is available, the application remains fully usable and the UI
reports that the assistant is unavailable.
"""
from __future__ import annotations

import functools
import http.client
import ipaddress
import json
import socket
import ssl
from urllib.parse import urlparse
from typing import Any
from urllib import error, request

from app.ai_local_config import resolve_base_url, resolve_model
from app.i18n import translate_text

OLLAMA_BASE_URL = resolve_base_url()
OLLAMA_MODEL = resolve_model()

_LOOPBACK_MESSAGE = "الاتصال بالنموذج مسموح فقط على الجهاز المحلي."


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # a redirect is an error, never followed


def _resolve_verified_loopback() -> list:
    """One resolution per request. Returns the verified (family, sockaddr) list that
    is the only thing the connection may dial (no second DNS lookup)."""
    parsed = urlparse(OLLAMA_BASE_URL)
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError(_LOOPBACK_MESSAGE)
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise RuntimeError(_LOOPBACK_MESSAGE) from exc
    pinned = []
    for family, _type, _proto, _canon, sockaddr in infos:
        try:
            loopback = ipaddress.ip_address(sockaddr[0].split("%")[0]).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise RuntimeError(_LOOPBACK_MESSAGE)
        pinned.append((family, sockaddr))  # full sockaddr keeps IPv6 flowinfo/scope_id
    if not pinned:
        raise RuntimeError(_LOOPBACK_MESSAGE)
    return pinned


def _ensure_loopback() -> None:
    _resolve_verified_loopback()


def _dial(pinned, timeout):
    last = None
    for family, sockaddr in pinned:
        sock = None
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            sock.connect(sockaddr)  # numeric sockaddr: no DNS
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            last = exc
            if sock is not None:
                sock.close()
            continue
        except BaseException:
            if sock is not None:
                sock.close()
            raise
        return sock
    raise last or OSError("no verified loopback address connected")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host, *args, pinned, **kwargs):
        super().__init__(host, *args, **kwargs)
        self._pinned = pinned

    def connect(self):
        self.sock = _dial(self._pinned, self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, *args, pinned, **kwargs):
        super().__init__(host, *args, **kwargs)
        self._pinned = pinned

    def connect(self):
        raw = _dial(self._pinned, self.timeout)
        try:
            # Name and certificate are checked against the ORIGINAL host, not the dialled IP.
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise  # TLS failure aborts; no fallback to another address


class _PinnedHTTPHandler(request.HTTPHandler):
    def __init__(self, pinned):
        super().__init__()
        self._pinned = pinned

    def http_open(self, req):
        if req.has_proxy():
            raise RuntimeError(_LOOPBACK_MESSAGE)
        return self.do_open(functools.partial(_PinnedHTTPConnection, pinned=self._pinned), req)


class _PinnedHTTPSHandler(request.HTTPSHandler):
    def __init__(self, pinned, context):
        super().__init__(context=context)
        self._pinned = pinned
        self._ctx = context

    def https_open(self, req):
        if req.has_proxy():
            raise RuntimeError(_LOOPBACK_MESSAGE)
        return self.do_open(functools.partial(_PinnedHTTPSConnection, pinned=self._pinned),
                            req, context=self._ctx)


def _pinned_opener():
    pinned = _resolve_verified_loopback()
    context = ssl.create_default_context()  # CERT_REQUIRED + check_hostname; never weakened
    # ProxyHandler({}) (not None) ignores environment and Windows registry proxies.
    return request.build_opener(request.ProxyHandler({}), _NoRedirect(),
                                _PinnedHTTPHandler(pinned), _PinnedHTTPSHandler(pinned, context))


TIMEOUT_SECONDS = 1.5


def _post_json(path: str, payload: dict[str, Any], timeout: float = TIMEOUT_SECONDS) -> dict[str, Any]:
    opener = _pinned_opener()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        f"{OLLAMA_BASE_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def local_model_status(language: str = "ar") -> dict[str, Any]:
    try:
        opener = _pinned_opener()
    except RuntimeError as exc:
        return {"available": False, "server": False, "model": OLLAMA_MODEL, "message": translate_text(str(exc), language) if str(exc) == _LOOPBACK_MESSAGE else str(exc)}
    """Return availability without ever contacting a remote service."""
    req = request.Request(f"{OLLAMA_BASE_URL}/api/tags", method="GET")
    try:
        with opener.open(req, timeout=0.8) as response:
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
    except (OSError, ValueError, error.URLError, http.client.HTTPException):
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
    from app.ai_hr_queries import explicit_unsupported_answer
    unsupported = explicit_unsupported_answer(question, language=language)
    if unsupported is not None:
        return unsupported
    status = local_model_status(language)
    if not status["available"]:
        raise RuntimeError(status["message"])
    try:
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
    except (OSError, http.client.HTTPException, ValueError) as exc:  # incl. IncompleteRead / undecodable body
        raise RuntimeError(translate_text("لم يستجب النموذج المحلي في الوقت المناسب.", language)) from exc
    message = response.get("message") if isinstance(response, dict) else None
    answer = str(message.get("content", "") if isinstance(message, dict) else "").strip()
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
    from app.ai_hr_queries import explicit_unsupported_answer
    unsupported = explicit_unsupported_answer(question, language=language)
    if unsupported is not None:
        return unsupported
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
    try:
        response = _post_json(
            "/api/chat",
            {"model": OLLAMA_MODEL, "stream": False, "format": "json",
             "messages": [{"role": "system", "content": system_prompt},
                          {"role": "user", "content": user_prompt}]},
            timeout=30,
        )
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise RuntimeError("لم يستجب النموذج المحلي في الوقت المناسب.") from exc
    message = response.get("message") if isinstance(response, dict) else None
    raw = str(message.get("content", "") if isinstance(message, dict) else "").strip()
    if not raw:
        raise RuntimeError("لم يُرجع النموذج المحلي خطة أمر.")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("النموذج المحلي لم يُرجع JSON صالحًا.") from exc
