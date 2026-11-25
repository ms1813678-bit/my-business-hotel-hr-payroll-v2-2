> Historical record below. Current branch, candidate identities, test outcomes and open work are in [CURRENT_SOURCE_STATUS.md](CURRENT_SOURCE_STATUS.md), updated25November2026. Old HEAD/readiness/pending-publication statements below describe their original checkpoint, not current state. No history has been removed.

# خريطة مقارنة ودمج الفروع — Version 2.2

تاريخ المراجعة: 2026-11-20

## المراجع الثابتة
- المستودع: https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2
- قاعدة التفرع المشتركة: `5d51017335a3437b0e8dde8a1df55e7ea88db7c0`
- `main` عند المراجعة: `2b0b278c3664bf6a4bbb63faf9d69999433f1cb7`
- فرع V2.2 قبل تحديث هذه الخريطة: `5cdf6c84efa5b3fa91cd73e057607f1a8496f9fd`
- المقارنة: `main` متقدم 13 Commit، وفرع V2.2 متقدم 24 Commit عن قاعدة التفرع. الفروع متباعدة وليست fast-forward.
- لا يوجد دمج آلي ولا استبدال لفرع بآخر.

## قرارات المراجعة حسب مجموعة التغييرات

| مجموعة Commits في main | ما تضيفه | حالة V2.2 | القرار |
|---|---|---|---|
| `2cf1939` CSS حديث | تصميم RTL حديث، بطاقات، نماذج، جداول، تجاوب | ملف V2.2 CSS أطول ويحتوي 152 selector تقريبًا مع دعم RTL/LTR/Chinese وتنسيقات إضافية | الاحتفاظ بـ CSS V2.2؛ لا نستبدله بملف main الأقصر |
| `023f1a6` base layout | شريط جانبي ومجموعات تنقل | V2.2 لديه نفس فكرة الشريط، ويضيف مبدّل اللغة وروابط الوحدات الفعلية | الاحتفاظ بقاعدة V2.2؛ عدم نسخ main فوقها |
| `2b0b278`, `e564ebb` AI UI | شاشة مساعد أكثر تنظيمًا، معاينة قديم/جديد ومخاطر | V2.2 لديه واجهة أبسط لكن تدفقها مختلف | نقل التصميم انتقائيًا بعد مواءمة المسارات: إرسال `/ai`، موافقة `/ai/approve`، رفض `/ai/reject`. لا تستخدم `/ai/prepare` أو `/ai/confirm` من main دون تعديل |
| `9cb57e8` login UI | صفحة دخول مصقولة | V2.2 لديه صفحة دخول مع مبدّل لغة وCSRF وتوافق AR/EN/ZH | الاحتفاظ بصفحة V2.2 وإزالة روابط Google Fonts الخارجية؛ إضافة اختبار يمنعها |
| `874b4d9` employee code allocator | تخصيص MY-0001 بتسلسل PostgreSQL غير قابل لإعادة الاستخدام | V2.2 لديه allocator نفسه، مع توضيح أن SQLite للاختبارات فقط | لا نقل؛ راجع migrations والاختبارات فقط في بوابة قواعد الأكواد |
| `f8da089` feature integration | ربط الإجازات والسلف والجزاءات والرواتب وshift matching | V2.2 يحتوي workflows مستقلة واختبارات موسعة لهذه الوحدات | الميزة مستوعبة في V2.2؛ لا نسخ ملفات main |
| `e40e944` AI confirmation enrichment | إثراء old/new/risks للعرض | V2.2 لديه `build_confirmation_preview` ولقطة بيانات حالية وإعادة تحقق عند الموافقة | V2.2 أكثر تقدمًا؛ لا تستبدل gateway. اختبر أن كل عملية لها معاينة فعلية |
| `03bd136`, `9d1eb98`, `0b95b7c`, `ad24470` وثائق التسليم | ادعاءات جاهزية V2.1 وقوائم قبول | وثائق V2.2 تختلف في النطاق والاختبارات | لا تنقل ادعاء الجاهزية. نستفيد فقط من بنود القبول بعد التحقق من ملاءمتها |
| `7b9bd88` version bump | إصدار 2.1.0 | V2.2 مضبوط على 2.2.0 | لا نقل |

## فروق معمارية حاسمة

1. `main/app/main.py` أصغر كثيرًا من `V2.2/app/main.py`؛ main لا يحتوي كل وحدات HR والرواتب الموجودة في فرع V2.2. ممنوع استبدال الملف.
2. `main/app/ai_agent.py` و`main/app/ai_gateway.py` أقدم وأقل توسعًا من نسختي V2.2. V2.2 يربط أوامر كثيرة بـ domain workflows ويعيد فحص snapshot قبل التنفيذ.
3. ملف `main/app/templates/ai.html` يستخدم endpoints لا تطابق تدفق V2.2 كما هو. يجب تعديلها واختبارها قبل نقل التصميم.
4. V2.2 يحتوي اللغة الصينية المبسطة وتبديل اللغة عبر Cookie، ويجب الحفاظ عليه.
5. V2.2 CSS لا يعتمد على CDN؛ صفحات HTML يجب ألا تضيف خطوطًا أو مكتبات خارجية.

## نتيجة المرحلة P1

- **لا حاجة إلى دمج عشوائي للـ 13 Commit.**
- الميزات الوظيفية الأساسية وallocator وAI gateway موجودة بالفعل في V2.2 بصيغ أوسع.
- التغيير المرشح الوحيد للنقل المرئي هو تحسينات AI UI، بعد إعادة كتابة actions لتتوافق مع endpoints V2.2 واختبار الحالات: سؤال عادي، أمر ينتج pending، موافقة، رفض، token منتهي، plan stale، وAR/EN/ZH.
- صفحة login لا تستبدل؛ فقط أزيل اعتماد الخط الخارجي من V2.2.
- CSS وbase يحتفظ بهما V2.2.

## المراجع / Commits main-only
- `2b0b278c3664bf6a4bbb63faf9d69999433f1cb7` AI assistant UI.
- `023f1a6a31aa0488b87d703d476a4cf9db1b6593` base layout.
- `2cf19394db66f8dc378e30b6e47b52b46087054` CSS.
- `9cb57e83faffbb371f5b338bbaa5bbc5c5616ab4` login.
- `03bd136212574272cd92ede5f7a6e0e3c7c1c7ff` delivery status.
- `874b4d9361109d998a6a64c9f825aa00a1a03c2c` employee code allocator.
- `f8da08969eb0a2f3beae8ccd90564438f9e9a790` V2.1 feature integration.
- `9d1eb98257c11fba77c5c2ac65e2d56b5274953a` acceptance checklist.
- `e564ebbe0639f6181bc41067ef068b8cb53fd70f` AI UI polish.
- `e40e944204fe30b43e2d903d960f2915283db9ed` AI gateway display enrichment.
- `0b95b7c8d10c92e3b068be60075526aa0f9804f6` V2.1 status.
- `7b9bd887fccca6d2e6d40ddbf94e90bbb936d8d3` version bump.
- `ad24470bbfb18a63e41c77e59b925826c6f96f2f` V2.1 rename/status.
