"""In-app usage guide: how-to answers grounded in the current version.

Each topic maps natural-language how-to questions (AR/EN/ZH) to the actual
steps of THIS version and the page that performs them. Answers are curated
against the real routes and workflows; nothing is invented by the model and
no data is read or changed.
"""
from __future__ import annotations

from typing import Any

# Each topic: keyword phrases (casefolded substring match) + the page path +
# trilingual step text. Paths are the real routes of this version.
_TOPICS: list[dict[str, Any]] = [
    {
        "keys": ("أصحح حضور", "تصحيح حضور", "تعديل حضور", "حضور يدوي", "انسى البصمة", "نسي البصمة",
                 "correct attendance", "fix attendance", "manual attendance", "forgot to punch",
                 "补打卡", "修改考勤", "手工考勤"),
        "path": "/attendance/review",
        "ar": "لتصحيح حضور موظف: افتح صفحة الحضور والانصراف، اختر اليوم، راجع سجل الموظف، ثم استخدم «حضور يدوي» وأدخل وقت الدخول والخروج والسبب. السجل يبقى بانتظار المراجعة حتى تعتمده، وأي تعديل يُحفظ في سجل المراجعة.",
        "en": "To correct an employee's attendance: open the Attendance page, pick the day, review the employee's record, then use \"Manual attendance\" and enter the clock-in, clock-out and reason. The record stays pending review until you approve it, and every change is kept in the audit log.",
        "zh": "更正员工考勤：打开「考勤」页面，选择日期，查看该员工记录，然后使用「手工考勤」输入上下班时间和原因。记录将保持待审核状态，直到您批准；所有修改都会保留在审计日志中。",
    },
    {
        "keys": ("أضيف موظف", "اضافة موظف", "إضافة موظف", "موظف جديد",
                 "add employee", "new employee", "hire",
                 "添加员工", "新员工", "新增员工"),
        "path": "/employees",
        "ar": "لإضافة موظف جديد: افتح صفحة الموظفين، اضغط «إضافة موظف»، أكمل البيانات الأساسية (الاسم، القسم، المسمى، الراتب، ساعات العمل) ثم احفظ. الكود يُنشأ تلقائيًا وتبدأ فترة خدمة جديدة من تاريخ التعيين.",
        "en": "To add a new employee: open the Employees page, click \"Add employee\", fill in the basics (name, department, position, salary, daily hours) and save. The code is generated automatically and a new service period starts from the hire date.",
        "zh": "添加新员工：打开「员工」页面，点击「添加员工」，填写基本信息（姓名、部门、职位、薪资、每日工时）并保存。工号自动生成，新的服务期从入职日期开始。",
    },
    {
        "keys": ("إجازة لموظف", "اجازة لموظف", "أضيف إجازة", "طلب إجازة", "رصيد إجازة",
                 "add leave", "leave request", "leave balance",
                 "添加请假", "请假申请", "申请请假", "请假余额"),
        "path": "/leave",
        "ar": "لتسجيل إجازة: افتح صفحة الإجازات، أنشئ طلبًا جديدًا وحدد الموظف ونوع الإجازة والتواريخ والأيام المحتسبة. الطلب يبدأ مسودة ولا يُخصم الرصيد إلا بعد اعتماده. رصيد الإجازة يظهر من نفس الصفحة.",
        "en": "To record leave: open the Leave page, create a new request with the employee, leave type, dates and charged days. The request starts as a draft and the balance is only deducted after approval. The leave balance is shown on the same page.",
        "zh": "登记请假：打开「请假」页面，新建申请并选择员工、请假类型、日期和计扣天数。申请以草稿开始，批准后才扣减余额。请假余额在同一页面查看。",
    },
    {
        "keys": ("سلفة", "سلف", "advance",
                 "预支", "借款"),
        "path": "/advances",
        "ar": "للسلف: افتح صفحة السلف، أنشئ طلب السلفة بالمبلغ والنوع، ثم اعتمده وحدد عدد الأقساط. بعد الاعتماد سجّل صرف السلفة للموظف من نفس الصفحة. قسط السلفة لا يُخصم من المسير إلا بعد إغلاقه.",
        "en": "For advances: open the Advances page, create the request with amount and type, approve it and set the installment count, then record payment from the same page. The advance installment is only deducted from a payroll run after the run is closed.",
        "zh": "预支：打开「预支」页面，创建含金额和类型的申请，批准并设置分期数，然后在同一页面登记支付。预支分期只有在工资批次关闭后才从中扣除。",
    },
    {
        "keys": ("مسير رواتب", "اعتماد المسير", "إغلاق الشهر", "اغلاق الشهر", "صرف المرتبات", "صرف الرواتب",
                 "payroll run", "approve payroll", "close the month", "pay salaries",
                 "工资批次", "批准工资", "关闭月份", "发放工资"),
        "path": "/payroll",
        "ar": "دورة المسير: أنشئ مسير الشهر من صفحة مسيرات الرواتب، راجع بنود الموظفين وعدّل ما يلزم وهو مسودة، اعتمد المسير، أغلق الشهر، ثم سجّل الصرف نقدًا أو تحويلًا لكل بند. إعادة فتح مسير مغلق تتطلب سببًا يُحفظ في سجل المراجعة. وتقدر تجرب «محاكاة ماذا لو» من صفحة المسير قبل أي قرار.",
        "en": "Payroll cycle: create the month's run from the Payroll Runs page, review and adjust employee items while it is a draft, approve the run, close the month, then record payment per item (cash or bank transfer). Reopening a closed run requires a reason kept in the audit log. You can try the \"What-if simulation\" on the run page before any decision.",
        "zh": "工资流程：在「工资批次」页面创建当月批次，草稿状态下核对并调整员工条目，批准批次，关闭月份，然后逐条登记支付（现金或银行转账）。重新打开已关闭的批次需要填写原因并保留在审计日志中。做决定前可以在批次页面使用「假设模拟」。",
    },
    {
        "keys": ("جدول الأمن", "ورديات الأمن", "مطابقة الأمن",
                 "security roster", "security shifts", "guard schedule",
                 "保安排班", "保安班表", "安保排班"),
        "path": "/security-roster",
        "ar": "جدول الأمن: من صفحة جدول الأمن ارفع ملف Excel (.xlsx) فيه التاريخ وأسماء موظفي الأمن ووقت بداية الوردية أو OFF. المعاينة لا تحفظ شيئًا؛ راجع ورديات كل يوم ثم اعتمده يومًا بيوم بعد تأشير «راجعت ورديات هذا اليوم». وتقدر تسأل المساعد «قارن جدول الأمن مع البصمة» فيعرض المطابقات والفروق لأي يوم.",
        "en": "Security roster: on the Security Roster page, upload an Excel (.xlsx) file with the date, the registered security employees' names, and each shift start time or OFF. The preview saves nothing; review each day's shifts, then approve day by day after ticking \"I reviewed this day's shifts\". You can also ask the assistant to \"compare the security roster with fingerprint punches\" to see matches and mismatches for any day.",
        "zh": "保安排班：在「保安排班」页面上传 Excel（.xlsx）文件，包含日期、已登记保安姓名和每班开始时间或 OFF。预览不会保存任何内容；逐日检查班次，勾选「我已检查当日班次」后逐日批准。您还可以让助手「对比保安排班和打卡记录」查看任意日期的匹配与差异。",
    },
    {
        "keys": ("جزاء", "مكافأة لموظف", "إجراء موظف", "خصم جزاء",
                 "sanction", "reward", "staff action", "disciplinary",
                 "处分", "奖励", "员工措施"),
        "path": "/staff-actions",
        "ar": "الجزاءات والمكافآت: من صفحة الجزاءات والمكافآت أنشئ إجراء الموظف (نوعه وتصنيفه وأثره المالي إن وجد) ثم اعتمده. الإجراءات المالية المعتمدة المستحقة تُدرج عند إنشاء مسير الشهر؛ المسيرات الموجودة بالفعل لا تتحدث تلقائيًا، فراجعها من صفحة الرواتب بشكل منفصل. الإلغاء يتطلب سببًا.",
        "en": "Sanctions and rewards: on the Sanctions & Rewards page, create the staff action (kind, category, financial effect if any) and approve it. Eligible approved financial actions are included when the month's payroll run is created; an already-created run is not refreshed automatically, so review it separately from the Payroll page. Cancellation requires a reason.",
        "zh": "奖惩：在「奖惩」页面创建员工措施（类型、类别、财务影响如有）并批准。符合条件的已批准财务措施会在创建当月工资批次时计入；已创建的工资批次不会自动更新，请在「工资」页面单独核对。取消需填写原因。",
    },
    {
        "keys": ("نسخة احتياطية", "النسخ الاحتياطي", "باك اب", "backup",
                 "备份"),
        "path": "/settings/backup",
        "ar": "النسخ الاحتياطي: من صفحة النسخ الاحتياطي أنشئ نسخة مشفرة من قاعدة البيانات واحتفظ بها في مكان آمن. النسخ لا يحذف أي بيانات من النظام.",
        "en": "Backup: on the Backup page, create an encrypted copy of the database and keep it somewhere safe. Backup never deletes any data from the system.",
        "zh": "备份：在「备份」页面创建数据库的加密副本并妥善保存。备份不会删除系统中的任何数据。",
    },
    {
        "keys": ("كلمة المرور", "كلمة السر", "غير الباسورد", "password", "change password",
                 "密码", "修改密码"),
        "path": "/settings/password",
        "ar": "لتغيير كلمة المرور: من قائمة النظام افتح «تغيير المرور»، أدخل الحالية ثم الجديدة مرتين واحفظ. الجلسات الأخرى تُنهى تلقائيًا بعد التغيير.",
        "en": "To change the password: from the System section open \"Change Password\", enter the current one, then the new one twice, and save. Other sessions are ended automatically after the change.",
        "zh": "修改密码：在「系统」栏目中打开「修改密码」，输入当前密码，再输入两次新密码并保存。修改后其他会话会自动退出。",
    },
]

_HOW_TO_MARKERS = (
    "إزاي", "ازاي", "كيف", "كيفية", "طريقة", "أعمل", "اعمل", "أضيف", "أسجل", "اسجل",
    "how do i", "how to", "how can i", "steps to",
    "怎么", "如何", "怎样",
)


def is_usage_guide_request(question: str) -> dict[str, Any] | None:
    """Match a how-to question to a curated topic, or return None.

    A how-to marker plus a topic keyword is required, so data questions
    (\"كم عدد...\", \"ملخص...\") keep routing to their own reporters.
    """
    q = " ".join((question or "").casefold().split())
    if not q:
        return None
    if not any(marker in q for marker in _HOW_TO_MARKERS):
        return None
    for topic in _TOPICS:
        if any(key in q for key in topic["keys"]):
            return topic
    return None


def format_usage_guide(topic: dict[str, Any], language: str = "ar") -> str:
    """Trilingual answer: the topic's steps plus a link to the right page."""
    steps = {"ar": topic["ar"], "en": topic["en"], "zh": topic["zh"]}.get(language, topic["ar"])
    link_label = {"ar": "فتح الصفحة", "en": "Open the page", "zh": "打开页面"}.get(language, "فتح الصفحة")
    return f"{steps}\n{link_label}: {topic['path']}"
