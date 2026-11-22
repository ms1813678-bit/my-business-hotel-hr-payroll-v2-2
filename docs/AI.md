# المساعد الذكي المحلي — الدليل

> English and Chinese sections follow below. / 下方有中文说明。

## العربية

### ما هو المساعد؟
مساعد ذكاء اصطناعي يعمل **محليًا بالكامل على جهازك** عبر Ollama. لا تغادر بيانات الفندق الجهاز إلى أي خدمة خارجية، ولا يحتاج المساعد اتصالًا بالإنترنت.

### ماذا يستطيع؟
- **يسأل ويجيب**: أرقام وحالات مباشرة من قاعدة البيانات (موظفون، حضور، إجازات، رواتب، سلف). الإجابات عن هذه الأسئلة تُبنى بقواعد ثابتة من البيانات نفسها، وليست تخمينًا من النموذج.
- **الفحص الذكي**: بطاقة أعلى صفحة المساعد تفحص طوابير العمل لحظيًا. «لا شيء معلّق» تعني أن الطابور فارغ الآن فقط، وأي فحص يتعذر إجراؤه يظهر كـ«غير مؤكد».
- **مراجعة ما قبل اعتماد الرواتب**: اكتب «راجع الرواتب قبل الاعتماد» مع الشهر (مثال: 2026-11) لتحصل على: حضور غير محسوم، أيام مكلَّف بها بدون سجل حضور، وخصومات غير معتادة إحصائيًا مقارنة بتاريخ الموظف — كلها اقتراحات مراجعة فقط، ولا تغيّر ولا تعتمد شيئًا. معايير المقارنة معلنة في نص المراجعة نفسها وهي إرشادات قابلة للضبط، وليست سياسة معتمدة.
- **ينفذ بأمان**: أوامر التعديل تتحول إلى خطة معروضة (القيم الحالية، التغييرات، الآثار)، ولا يتم أي تنفيذ قبل موافقتك الصريحة. كل خطة محفوظة ببصمة وبصلاحية محدودة.

### حدود معروفة
- إجابات النموذج الحر (خارج الأسئلة المهيكلة) قد تخطئ؛ النص غير المؤكد يُوسم بذلك. الحسابات المالية تأتي دائمًا من محركات النظام الثابتة، لا من النموذج.
- المحادثة محفوظة لكل مستخدم على جهازه (بحد أقصى 200 رسالة). كل رسالة تُعرض بلغتها الأصلية كما كُتبت، وتُوسم بلغتها إن اختلفت عن لغة الصفحة.
- نتائج تنفيذ الأوامر (بعد الموافقة) تُعرض بالعربية حاليًا حتى لو كانت الواجهة بإنجليزية أو صينية؛ تُوسم كذلك في المحادثة.
- اكتشاف أسئلة مقارنة جدول الأمن بالبصمات يتعرف على الصياغة العربية حاليًا، والتقرير نفسه يُعرض بلغة الواجهة.
- إن كان النموذج غير متاح، يعمل المساعد بالردود المهيكلة من قاعدة البيانات، والنظام الأساسي لا يتأثر.

### النموذج والجهاز
- الافتراضي: **qwen2.5:3b** (خفيف، يناسب جهازك: معالج i9-12900H وذاكرة 16GB). يُشغَّل عبر:
  - `ollama pull qwen2.5:3b`
  - `ollama serve` (يعمل تلقائيًا بعد التثبيت على ويندوز)
- **تغيير النموذج اختياري**: اضبط المتغير `OLLAMA_MODEL` على اسم نموذج آخر مثبت لديك (مثال: `qwen2.5:7b`). النماذج الأكبر قد تكون أبطأ على جهازك أو تستهلك ذاكرة أكبر؛ التأثير الفعلي يعتمد على جهازك وما يعمل عليه، فجرّب وقِس قبل الاعتماد. لا توجد أرقام أداء مضمونة.
- إن أردت العودة للوضع الخفيف: أعد `OLLAMA_MODEL` إلى `qwen2.5:3b`.

---

## English

### What it is
A local AI assistant running **entirely on your machine** via Ollama. Hotel data never leaves the device; no internet connection is needed.

### What it can do
- **Answer HR questions** with deterministic, database-built answers (not model guesses): employees, attendance, leave, payroll, advances.
- **Smart Scan**: a card at the top of the assistant page that live-checks the work queues. "Nothing pending" only means the queue is empty right now; a check that cannot run is shown as unverified.
- **Pre-payroll review**: ask for a pre-approval payroll review with a month (e.g. 2026-11) to get unsettled attendance, assigned days with no attendance record, and statistically unusual deductions vs the employee's own history — review suggestions only; nothing is changed or approved. The comparison criteria are stated in the review itself and are adjustable heuristics, not adopted HR policy.
- **Act safely**: change commands become a clear plan (current values, changes, risks) and nothing executes before your explicit approval. Plans are fingerprinted and expire.

### Known limits
- Free-form model answers (outside the structured questions) can be wrong; unverified model text is marked. Financial calculations always come from the system's deterministic engines, never from the model.
- Conversations are stored per user on the local device (capped at 200 messages). Each message renders in the language it was written in and is badged when that differs from the page language.
- Command execution results (after approval) are still generated in Arabic on English/Chinese pages; they are badged as such in the thread.
- Security-roster/fingerprint comparison questions are detected in Arabic phrasing for now; the report itself renders in the page language.
- If the model is unavailable, the assistant still answers the structured questions from the database and the core system is unaffected.

### Model and hardware
- Default: **qwen2.5:3b** (light; suits the target machine: i9-12900H, 16 GB RAM). Run with:
  - `ollama pull qwen2.5:3b`
  - `ollama serve` (starts automatically after install on Windows)
- **Changing the model is optional**: set `OLLAMA_MODEL` to another installed model (e.g. `qwen2.5:7b`). Larger models may run slower or use more memory on your machine; the real impact depends on your hardware and load, so try and measure before committing. No performance numbers are guaranteed.
- To return to the light default, set `OLLAMA_MODEL` back to `qwen2.5:3b`.

---

## 中文

### 简介
本地 AI 助手，通过 Ollama **完全在您的电脑上运行**。酒店数据不会离开本机，无需联网。

### 功能
- **人事问答**：员工、考勤、请假、工资、预支等问题的答案由数据库按固定规则生成，不是模型猜测。
- **智能检查**：助手页顶部的卡片实时检查工作队列。“暂无待处理”仅表示当前队列为空；无法执行的检查显示为未确认。
- **工资批准前检查**：输入“发薪前检查”并注明月份（如 2026-11），可得到未结考勤、已排班但无考勤记录的日期、以及与员工本人历史相比统计异常的扣款 — 仅供参考建议，不会更改或批准任何内容。对比标准在结果中公示，属于可调整的参考值，并非既定人事政策。
- **安全执行**：修改指令会生成清晰的计划（当前值、变更、影响），未经您明确批准不会执行。计划带有指纹且有时效。

### 已知限制
- 结构化问题之外的自由回答可能出错；未经验证的模型文本会标注。财务计算始终来自系统固定引擎，而非模型。
- 对话按用户保存在本机（上限 200 条）。每条消息以原文语言显示；与页面语言不同时会标注原文语言。
- 指令执行结果（批准后）目前仍以阿拉伯语生成；在对话中会标注原文语言。
- 安保排班与指纹对比的问题目前识别阿拉伯语表述；报告本身以页面语言显示。
- 模型不可用时，助手仍可用数据库回答结构化问题，核心系统不受影响。

### 模型与硬件
- 默认：**qwen2.5:3b**（轻量，适合目标设备：i9-12900H、16GB 内存）。运行方式：
  - `ollama pull qwen2.5:3b`
  - `ollama serve`（Windows 安装后自动运行）
- **更换模型（可选）**：将 `OLLAMA_MODEL` 设为已安装的其他模型（如 `qwen2.5:7b`）。更大的模型在您的设备上可能更慢或占用更多内存；实际影响取决于硬件和负载，请先试用再决定。不保证任何性能数字。
- 恢复轻量默认：将 `OLLAMA_MODEL` 改回 `qwen2.5:3b`。
