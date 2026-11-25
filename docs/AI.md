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
- المحادثة محفوظة لكل مستخدم على جهازه ولا تُحذف آليًا أبدًا (العرض الأخير محدود بعدد، والسجل الكامل محتفَظ به). الرسائل الجديدة تُوسم بلغتها الفعلية: رسائل المستخدم تُتحدد من حروفها لا من لغة الواجهة، ورسائل النظام بلغة توليدها. الرسائل القديمة قبل هذه الميزة موسومة «غير معروفة» وتُعرض بدون وسم لغة. النصوص الأصلية لا تُعدَّل.
- نتائج تنفيذ الأوامر (بعد الموافقة) تُعرض بالعربية حاليًا حتى لو كانت الواجهة بإنجليزية أو صينية؛ تُوسم كذلك في المحادثة.
- اكتشاف أسئلة مقارنة جدول الأمن بالبصمات يتعرف على الصياغة العربية حاليًا، والتقرير نفسه يُعرض بلغة الواجهة.
- إن كان النموذج غير متاح، يعمل المساعد بالردود المهيكلة من قاعدة البيانات، والنظام الأساسي لا يتأثر.

### النموذج والجهاز
- الافتراضي: **qwen2.5:3b** هو الاسم الافتراضي الحالي فقط، وليس اعتمادًا للإنتاج أو إثبات أداء على جهاز الفندق. ترخيص هذا الحجم بحثي؛ يلزم نموذج ذو ترخيص مناسب واختبار جودة وأداء قبل الاعتماد. أوامر التشغيل التقنية:
  - `ollama pull qwen2.5:3b`
  - `ollama serve` (يعمل تلقائيًا بعد التثبيت على ويندوز)
- **تغيير النموذج اختياري**: اضبط `LOCAL_AI_MODEL` (أو الاسم البديل `OLLAMA_MODEL`) في ملف `.env` أو في متغيرات البيئة على اسم نموذج آخر مثبت لديك (مثال: `qwen2.5:7b`)، ثم أعد تشغيل التطبيق. متغيرات البيئة لها الأولوية على `.env`، وإن كان الاسم غير صالح يُستخدم الافتراضي. النماذج الأكبر قد تكون أبطأ على جهازك أو تستهلك ذاكرة أكبر؛ التأثير الفعلي يعتمد على جهازك وما يعمل عليه، فجرّب وقِس قبل الاعتماد. لا توجد أرقام أداء مضمونة.
- إن أردت العودة للوضع الخفيف: أعد `LOCAL_AI_MODEL` إلى `qwen2.5:3b` (أو احذف السطر).

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
- Conversations are stored per user on the local device and are never auto-deleted (recent reads are bounded; the full history is retained). New messages are labeled with their actual language: user messages from their own script, not the interface locale; system messages by their generation language. Messages from before this feature are marked "unknown" and show no language badge. Original texts are never rewritten.
- Command execution results (after approval) are still generated in Arabic on English/Chinese pages; they are badged as such in the thread.
- Security-roster/fingerprint comparison questions are detected in Arabic phrasing for now; the report itself renders in the page language.
- If the model is unavailable, the assistant still answers the structured questions from the database and the core system is unaffected.

### Model and hardware
- Default: **qwen2.5:3b** is the current configured default, not production approval or measured hotel-device performance. This size has a Research license. A suitable model license and measured quality/performance are required before production acceptance. Technical commands:
  - `ollama pull qwen2.5:3b`
  - `ollama serve` (starts automatically after install on Windows)
- **Changing the model is optional**: set `LOCAL_AI_MODEL` (or the alias `OLLAMA_MODEL`) in the `.env` file or the environment to another installed model (e.g. `qwen2.5:7b`), then restart the app. Environment variables take priority over `.env`; an invalid name falls back to the default. Larger models may run slower or use more memory on your machine; the real impact depends on your hardware and load, so try and measure before committing. No performance numbers are guaranteed.
- To return to the light default, set `LOCAL_AI_MODEL` back to `qwen2.5:3b` (or remove the line).

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
- 对话按用户保存在本机，永不自动删除（仅限制最近读取条数，完整记录保留）。新消息标注实际语言：用户消息按其文字脚本判断，而非界面语言；系统消息按其生成语言。此功能之前的旧消息标记为"未知"，不显示语言标注。原文永不改写。
- 指令执行结果（批准后）目前仍以阿拉伯语生成；在对话中会标注原文语言。
- 安保排班与指纹对比的问题目前识别阿拉伯语表述；报告本身以页面语言显示。
- 模型不可用时，助手仍可用数据库回答结构化问题，核心系统不受影响。

### 模型与硬件
- 默认：**qwen2.5:3b** 仅为当前默认配置，并非生产批准或酒店设备性能测试结果。此规格采用研究许可证；生产验收前须验证合适的模型许可、质量和性能。技术命令：
  - `ollama pull qwen2.5:3b`
  - `ollama serve`（Windows 安装后自动运行）
- **更换模型（可选）**：在 `.env` 文件或环境变量中将 `LOCAL_AI_MODEL`（别名 `OLLAMA_MODEL`）设为已安装的其他模型（如 `qwen2.5:7b`），然后重启应用。环境变量优先于 `.env`；名称无效时使用默认值。更大的模型在您的设备上可能更慢或占用更多内存；实际影响取决于硬件和负载，请先试用再决定。不保证任何性能数字。
- 恢复轻量默认：将 `LOCAL_AI_MODEL` 改回 `qwen2.5:3b`（或删除该行）。


Configuration precedence detail: an environment `OLLAMA_MODEL` alias takes priority over a `LOCAL_AI_MODEL` entry in the install `.env`, because the whole environment source wins. Blank values fall through to the next source; an invalid winning model name logs a warning and uses the unchanged default. The install `.env` accepts UTF-8 with or without a BOM. Missing reader or unreadable file logs a warning and uses environment/default settings. Restart after edits. This does not install/pull a model or approve its license/performance.

تفصيل الأولوية: الاسم البديل OLLAMA_MODEL في بيئة التشغيل يتقدم على LOCAL_AI_MODEL داخل ملف .env. القيم الفارغة تنتقل للمصدر التالي؛ الاسم غير الصالح في المصدر الأعلى يسجل تحذيرًا ويستخدم الافتراضي. ملف التثبيت يقبلUTF-8بعلامةBOMأو دونها. تعذر القراءة يسجل تحذيرًا. يلزم إعادة التشغيل؛ لا تنزيل نموذج ولا اعتماد ترخيصه أو أدائه.

配置优先级：环境变量 OLLAMA_MODEL 别名优先于安装目录 .env 中的 LOCAL_AI_MODEL。空值会尝试下一来源；最高优先级的无效模型名会记录警告并使用原默认值。文件支持带或不带 BOM 的 UTF-8。缺少读取器或文件不可读时会记录警告。修改后重启，不会下载模型，也不代表模型许可或性能获批。
