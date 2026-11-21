"""Offline UI translation and document direction handling.

Only known interface labels and selected presentation attributes are translated. Form values and input data are left untouched; dynamic text is translated only when it exactly matches a catalog entry.
"""
from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser


EN = {
"العربية":"Arabic","English":"English","中文":"Chinese","ماذا يستطيع المساعد؟":"What can the assistant do?","يسأل ويجيب":"Ask and answer","أرقام وحالات مباشرة من قاعدة البيانات: الموظفون، الحضور، الإجازات، الرواتب.":"Figures and statuses directly from the database: employees, attendance, leave, payroll.","ينفذ بأمان":"Acts safely","أوامر التعديل تتحول إلى خطة واضحة معروضة عليك، ولا يتم أي تنفيذ قبل موافقتك الصريحة.":"Modification commands become a clear plan shown to you; nothing executes before your explicit approval.","خصوصية كاملة":"Full privacy","كل المعالجة تتم على جهازك المحلي، ولا تغادر بيانات الفندق إلى أي خدمة خارجية.":"All processing happens on your local machine; hotel data never leaves to any external service.","اضغط على أي مثال لنسخه في مربع السؤال.":"Tap any example to copy it into the question box.","ما الذي يحتاج مراجعة في الحضور هذا الشهر؟":"What needs review in attendance this month?","نظام الموارد البشرية والرواتب":"HR & Payroll System",
"الرئيسية":"Home","لوحة التحكم":"Dashboard","مساعد الذكاء الاصطناعي":"AI Assistant",
"الموارد البشرية":"Human Resources","الموظفون":"Employees","الحضور والانصراف":"Attendance",
"الإجازات":"Leave","الجزاءات والمكافآت":"Sanctions & Rewards","جدول الأمن":"Security Roster",
"الرواتب والمالية":"Payroll & Finance","مسيرات الرواتب":"Payroll Runs","السلف":"Advances",
"التقارير":"Reports","النظام":"System","النسخ الاحتياطي":"Backup",
"تسجيل الدخول — نظام الموارد البشرية":"Sign in — HR System","تسجيل الدخول":"Sign In","شعار الفندق":"Hotel logo",
"اسم المستخدم":"Username","أدخل اسم المستخدم":"Enter your username","كلمة المرور":"Password",
"أدخل كلمة المرور":"Enter your password","دخول":"Sign In","تعليمات":"Instructions",
"يعمل محليًا على Windows 10 و 11 — ولا يُعرض على الإنترنت.":"Runs locally on Windows 10 and 11 — not exposed to the internet.",
"اسم المستخدم أو كلمة المرور غير صحيحة.":"The username or password is incorrect.",
"نظرة سريعة على حالة العمل والمهام التي تحتاج قرار مسؤول الموارد البشرية.":"A quick overview of operations and tasks requiring an HR administrator's decision.",
"مساعد AI المحلي":"Local AI Assistant","تسجيل الخروج":"Sign Out","الحالة الآن":"Current Status",
"أهلًا بك،":"Welcome,","كل الأرقام التالية مأخوذة مباشرة من قاعدة البيانات المحلية.":"All figures below are taken directly from the local database.",
"فتح التقارير":"Open Reports","مؤشرات رئيسية":"Key Metrics","ملخص النظام":"System Summary",
"الموظفون النشطون":"Active Employees","إدارة الموظفين ←":"Manage Employees ←","الموظفون السابقون":"Former Employees",
"السجل الوظيفي ←":"Employment Records ←","حضور يحتاج مراجعة":"Attendance Awaiting Review","مراجعة الحضور ←":"Review Attendance ←",
"إجازات غير مؤكدة":"Unconfirmed Leave Requests","فتح الإجازات ←":"Open Leave ←","سلف قيد الإجراء":"Advances in Progress",
"فتح السلف ←":"Open Advances ←","جزاءات ومكافآت للمراجعة":"Sanctions & Rewards Awaiting Review","فتح الإجراءات ←":"Open Actions ←",
"مسيرات رواتب مفتوحة":"Open Payroll Runs","فتح الرواتب ←":"Open Payroll ←","المهام":"Tasks","ما يحتاج متابعة":"Items Requiring Follow-up",
"استيراد الحضور والبصمة":"Import Attendance & Fingerprint Data","رفع الملفات الأصلية ومراجعة مشاكل الاستيراد.":"Upload source files and review import issues.",
"مطابقة الحضور الشهرية":"Monthly Attendance Reconciliation","مراجعة الفجوات والاستثناءات قبل الاعتماد.":"Review gaps and exceptions before approval.",
"مراجعة الرواتب":"Review Payroll","معاينة المسير قبل الاعتماد والإغلاق.":"Preview the payroll run before approval and closing.",
"حماية البيانات والتحقق من النسخ.":"Protect data and verify backups.","الوحدات":"Modules","الوصول السريع":"Quick Access",
"البيانات والهيكل الإداري":"Employee data and organization structure","الاستيراد والمراجعة والمطابقة":"Import, review, and reconciliation",
"الأرصدة والطلبات والسجل":"Balances, requests, and history","المعاينة والاعتماد والصرف":"Preview, approval, and payment",
"الطلبات والأقساط والخصومات":"Requests, installments, and deductions","التسجيل والاعتماد والتاريخ":"Recording, approval, and history",
"Excel وPDF وفترات مخصصة":"Excel, PDF, and custom date ranges","الاستيراد والتحقق اليومي":"Import and daily validation",
"ذكاء محلي اختياري":"Optional Local AI","مساعد AI للموارد البشرية":"HR AI Assistant",
"تحليل الأسئلة والبيانات بشكل محلي، مع مراجعة المسؤول قبل أي إجراء مؤثر.":"Analyze questions and data locally. Administrator review is required before any consequential action.",
"فتح المساعد":"Open Assistant","إدارة الموظفين":"Employee Management",
"عرض بطاقات منظم: بيانات واضحة وإجراءات مرتبة دون جداول طويلة.":"Organized employee cards with clear information and structured actions.",
"تسجيل جديد":"New Record","إضافة موظف":"Add Employee","الاسم بالكامل":"Full Name","الرقم القومي":"National ID","الجنسية":"Nationality","مصري":"Egyptian","أجنبي":"Foreign","رقم الباسبور (للأجانب)":"Passport Number (foreign)",
"رقم الهاتف":"Phone Number","العنوان":"Address","القسم":"Department","الفرع":"Branch","بدون فرع":"No Branch",
"المسمى الوظيفي":"Job Title","تاريخ التعيين":"Hire Date","بداية فترة الاختبار":"Probation Start Date",
"نهاية فترة الاختبار":"Probation End Date","الراتب الأساسي بالجنيه":"Basic Salary (EGP)","ساعات العمل اليومية":"Daily Working Hours",
"رقم البصمة":"Fingerprint ID","الصورة الشخصية (اختياري JPG/PNG/WEBP، حد 5MB)":"Profile Photo (optional JPG/PNG/WEBP, max 5 MB)",
"حفظ الموظف":"Save Employee","السجل":"Records","بطاقات الموظفين":"Employee Cards","نشط":"Active","الراتب الأساسي":"Basic Salary",
"ج.م":"EGP","ساعة":"hours","ساعات يومية":"Daily Hours","تعديل البيانات":"Edit Details","حفظ التعديلات":"Save Changes",
"نقل قسم / فرع":"Transfer Department / Branch","القسم الجديد":"New Department","الفرع الجديد":"New Branch",
"تاريخ سريان النقل":"Transfer Effective Date","سبب النقل":"Transfer Reason","تسجيل النقل":"Record Transfer",
"طلب تصحيح بيانات":"Request Data Correction","البيان":"Field","القيمة المطلوبة":"Requested Value","سبب الطلب":"Request Reason",
"تسجيل الطلب":"Submit Request","سجل التغييرات":"Change History","اسم القسم":"Department Name","إضافة قسم":"Add Department",
"الأقسام الحالية":"Current Departments","إضافة الفرع":"Add Branch","الأقسام والفروع":"Departments & Branches",
"اسم الفرع":"Branch Name","لا يوجد موظفون حاليون.":"There are no current employees.",
"تسجيل إجازة جديدة / New leave record":"New Leave Record","نوع الإجازة / Leave type":"Leave Type",
"الموظف / Employee":"Employee","من تاريخ / Start date":"Start Date","إلى تاريخ / End date":"End Date",
"أيام الخصم من الرصيد / Charged days":"Days Charged to Balance","السبب / Reason":"Reason","حفظ كمسودة / Save draft":"Save as Draft",
"تسجيل رصيد افتتاحي أو تسوية / Balance entry":"Opening Balance or Adjustment",
"الأرصدة الافتتاحية تدخل يدويًا حسب رصيد كل موظف. لا ينشئ النظام أي استحقاق تلقائي. التسوية الموجبة تزيد الرصيد والسالبة تخصمه.":"Opening balances are entered manually for each employee. The system does not create automatic accruals. Positive adjustments add to the balance; negative adjustments reduce it.",
"نوع الرصيد":"Balance Type","نوع الحركة":"Transaction Type","عدد الأيام":"Number of Days",
"تسجيل الحركة / Record balance":"Record Transaction","أرصدة الموظفين الحالية / Current balances":"Current Employee Balances",
"سجل الإجازات / Leave history":"Leave History","النوع":"Type","الفترة":"Period","أيام تقويمية":"Calendar Days",
"أيام خصم":"Charged Days","الحالة":"Status","الإجراء":"Action","اعتماد وخصم الرصيد":"Approve and Deduct Balance",
"سبب الإلغاء":"Cancellation Reason","إلغاء":"Cancel","لا توجد إجازات مسجلة بعد.":"No leave records yet.",
"أنواع الإجازات / Leave types":"Leave Types","يمكن إضافة نوع مخصص. تفعيل تتبع الرصيد يعني أن اعتماد الإجازة يخصم الأيام من رصيد الموظف.":"You can add a custom leave type. Enabling balance tracking means approved leave days are deducted from the employee's balance.",
"الكود / Code":"Code","الاسم بالعربية":"Arabic Name","الاسم بالإنجليزية":"English Name","مدفوعة / Paid":"Paid",
"تخصم من رصيد / Tracks balance":"Deduct from Balance","إضافة نوع / Add type":"Add Leave Type",
"آخر حركات الأرصدة / Recent balance transactions":"Recent Balance Transactions","التاريخ":"Date","الحركة":"Transaction",
"الأيام":"Days","لا توجد حركات أرصدة بعد.":"No balance transactions yet.",
"مراجعة واعتماد الحضور":"Attendance Review & Approval","إضافة حضور يدوي":"Add Manual Attendance",
"هذا الإدخال يسجل كحضور يدوي بواسطة مسؤول HR ولا يغيّر البصمة الأصلية. سيظل اليوم بحاجة إلى المراجعة والاعتماد قبل استخدامه في الرواتب.":"This records manual attendance by the HR administrator without changing the original fingerprint data. The day must still be reviewed and approved before payroll use.",
"الموظف":"Employee","اختر الموظف":"Select Employee","تاريخ العمل":"Work Date","وقت الدخول":"Clock-in Time",
"وقت الخروج":"Clock-out Time","سبب الإدخال اليدوي":"Manual Entry Reason","إضافة الحضور للمراجعة":"Add Attendance for Review",
"لا توجد سجلات معلقة":"No Pending Records","بعد إنشاء مسودة المطابقة ستظهر هنا الأيام التي تحتاج مراجعة الموارد البشرية.":"Days requiring HR review will appear here after a reconciliation draft is created.",
"مطابقة شهر جديد":"Reconcile New Month","اعتماد الشهور":"Approve Months","السابق":"Previous","التالي":"Next",
"الوردية:":"Shift:","غير محددة":"Not Specified","غير موجود":"Not Available","غير موجودة":"Not Available",
"غير مسجل":"Not Recorded","غير معروف":"Unknown","غير محسوبة":"Not Calculated","تقرير البرنامج:":"Program Report:",
"البصمة:":"Fingerprint:","إدخال HR اليدوي:":"Manual HR Entry:","سبب الإدخال:":"Entry Reason:",
"أدخله:":"Entered By:","وقت التسجيل:":"Recorded At:","الدقائق المقترحة:":"Suggested Minutes:","ملاحظات:":"Notes:",
"وقت الدخول المعتمد بعد المراجعة":"Approved Clock-in Time After Review","وقت الخروج المعتمد بعد المراجعة":"Approved Clock-out Time After Review",
"سبب المراجعة والاعتماد":"Review and Approval Reason","اعتماد هذا اليوم":"Approve This Day",
"مراجعة واعتماد شهور الحضور":"Review & Approve Attendance Months","مراجعة الأيام":"Review Days","لا توجد شهور مستوردة":"No Imported Months",
"استورد تقرير البرنامج وملف البصمة للفترة نفسها لإنشاء سجل الشهر.":"Import the program report and fingerprint file for the same period to create a monthly record.",
"حالة الشهر:":"Month Status:","سجلات الأيام:":"Daily Records:","المعتمد:":"Approved:","المعلّق:":"Pending:",
"أيام موظفين بلا سجل مطابقة:":"Employee-Days Without a Reconciliation Record:","عرض أمثلة فجوات التغطية (حتى 20)":"Show Coverage Gap Examples (up to 20)",
"مشكلات استيراد البرنامج:":"Program Import Issues:","مشكلات استيراد البصمة:":"Fingerprint Import Issues:","الملف غير مكتمل":"File Incomplete",
"سبب إغلاق الشهر":"Month Closing Reason","أؤكد أنني راجعت تغطية جميع الموظفين والأيام، وراجعت النواقص بين التقرير والبصمة، ولم أعتبر الخانات الفارغة غيابًا تلقائيًا.":"I confirm that I reviewed all employees and days, checked gaps between the report and fingerprint file, and did not treat blank entries as automatic absence.",
"اعتماد الشهر نهائيًا":"Approve Month Finally","تم اعتماد الشهر بواسطة:":"Month Approved By:","الشهر في انتظار اكتمال الاستيراد وإنشاء المطابقة.":"The month is waiting for import completion and reconciliation.",
"استيراد جدول الأمن":"Import Security Roster","رفع ملف Excel":"Upload Excel File",
"ارفع ملف .xlsx يحتوي على التاريخ وأسماء موظفي الأمن الثلاثة. اكتب وقت بداية الوردية أو OFF. المعاينة لا تحفظ أي ورديات.":"Upload an .xlsx file containing dates and security staff names. Enter each shift start time or OFF. Preview does not save any shifts.",
"رفع ومعاينة":"Upload and Preview","ملاحظات الاستيراد":"Import Issues","كل يوم يُراجع ويُعتمد منفصلًا.":"Each day is reviewed and approved separately.",
"تم اعتماد هذا اليوم بالفعل":"This day has already been approved","جاهز للاعتماد بعد المراجعة":"Ready for Approval After Review",
"يحتاج إلى مراجعة وتصحيح":"Requires Review and Correction","البداية":"Start","النهاية":"End","المدة":"Duration",
"راجعت ورديات هذا اليوم":"I have reviewed this day's shifts","اعتماد اليوم":"Approve Day","لن يتم حفظ تعيينات هذا اليوم.":"Assignments for this day will not be saved.",
"استيراد الحضور والانصراف":"Import Attendance","رفع تقرير الحضور أو البصمة":"Upload Attendance or Fingerprint Report",
"نوع الملف":"File Type","تقرير برنامج الحضور (المصدر الأساسي)":"Attendance Program Report (Primary Source)",
"تقرير جهاز البصمة (استكمال النواقص)":"Fingerprint Device Report (Fill Missing Data)","ملف الحضور / Attendance file":"Attendance File",
"استيراد وحفظ الأدلة للمراجعة":"Import and Save Evidence for Review",
"الحد الأقصى لحجم الملف 5 ميجابايت. لا يتم تخمين هوية موظف غير معروف.":"Maximum file size is 5 MB. Unknown employee identities are never guessed.",
"تعذر الاستيراد:":"Import Failed:","تم تسجيل دفعة الاستيراد":"Import Batch Recorded","رقم الدفعة:":"Batch Number:",
"تحميل نسخة الملف الأصلي المحفوظة":"Download Saved Original File","عدد أحداث الدخول/الخروج المستخرجة:":"Extracted Clock-in/Clock-out Events:",
"عدد الملاحظات التي تحتاج مراجعة:":"Issues Requiring Review:","تم الاستيراد وتنتظر المطابقة":"Imported — Awaiting Reconciliation",
"مراجعة مشكلات استيراد الحضور":"Review Attendance Import Issues","العودة للاستيراد":"Back to Import","تعذر تنفيذ الإجراء:":"Action Failed:",
"الملف:":"File:","الملاحظات غير المحسومة:":"Unresolved Issues:","تحميل الملف الأصلي":"Download Original File",
"المشكلة رقم":"Issue Number","الورقة:":"Sheet:","الصف:":"Row:","القيمة الأصلية:":"Original Value:",
"غير محددة":"Not Specified","غير محدد":"Not Specified","غير متاحة":"Not Available","تم الحل:":"Resolved:","بواسطة":"By","في":"At",
"اختر الموظف الصحيح بعد مراجعة الاسم/الكود الأصلي":"Select the correct employee after checking the original name/code",
"تأكيد ربط البصمات بهذا الموظف وتسجيل المراجعة":"Confirm Fingerprint Link and Record Review",
"هذه المشكلة لا يمكن حلها بتغيير هوية الموظف. راجع المصدر الأصلي وصحح الملف ثم أعد استيراده.":"This issue cannot be resolved by changing the employee identity. Check the source, correct the file, and import it again.",
"لا توجد مشكلات مسجلة في هذه الدفعة.":"No issues are recorded in this batch.",
"مطابقة الحضور الشهرية":"Monthly Attendance Reconciliation","إنشاء مسودة مراجعة الشهر":"Create Monthly Review Draft",
"دفعة تقرير البرنامج":"Program Report Batch","دفعة جهاز البصمة":"Fingerprint Device Batch","اختر ملف البرنامج":"Select Program File",
"اختر ملف البصمة":"Select Fingerprint File","بداية الفترة":"Period Start","نهاية الفترة":"Period End",
"إنشاء مسودة المطابقة":"Create Reconciliation Draft","تعذر إنشاء المطابقة:":"Reconciliation Failed:","تم إنشاء مسودة المراجعة":"Review Draft Created",
"الفترة:":"Period:","إجمالي سجلات الأيام:":"Total Daily Records:","جاهزة لمراجعة الموارد البشرية:":"Ready for HR Review:",
"تحتاج مراجعة أو استكمال:":"Require Review or Completion:","ملاحظات الاستيراد في الملفين:":"Import Issues in Both Files:",
"لم يتم اعتماد أي يوم أو استخدامه في الرواتب.":"No day has been approved or used in payroll.",
"فتح قائمة مراجعة الحضور":"Open Attendance Review","تسجيل جزاء أو مكافأة / New staff action":"Record a Sanction or Reward",
"القيم المالية يحددها مسؤول الموارد البشرية يدويًا. لا توجد معادلات تلقائية للمكافآت أو الجزاءات. يجب تحديد شهر الرواتب للإجراء المالي.":"Financial amounts are entered manually by the HR administrator. There are no automatic reward or sanction calculations. A payroll month is required for a financial action.",
"نوع الإجراء / Type":"Action Type","جزاء / Sanction":"Sanction","مكافأة / Reward":"Reward","التصنيف / Category":"Category",
"الجزاءات / Sanctions":"Sanctions","تنبيه شفهي / Verbal notice":"Verbal Notice","إنذار أول / First warning":"First Warning",
"إنذار نهائي / Final warning":"Final Warning","خصم من الراتب / Salary deduction":"Salary Deduction","إيقاف عن العمل / Suspension":"Suspension",
"المكافآت / Rewards":"Rewards","مكافأة مالية / Financial reward":"Financial Reward","شهادة تقدير / Certificate":"Certificate of Appreciation",
"إشادة / Commendation":"Commendation","توصية بالترقية / Promotion recommendation":"Promotion Recommendation","نوع مخصص / Custom":"Custom",
"عنوان الإجراء / Title":"Action Title","شهر الرواتب / Payroll month":"Payroll Month","المبلغ بالجنيه / Amount (EGP)":"Amount (EGP)",
"إجراء مالي / Financial item":"Financial Item","السبب والتفاصيل / Reason":"Reason and Details","حفظ كمسودة / Save draft":"Save as Draft",
"سجل الجزاءات والمكافآت / History":"Sanctions & Rewards History","القيمة":"Amount","شهر الراتب":"Payroll Month",
"إفادة الموظف":"Employee Acknowledgement","الإجراءات":"Actions","تم إطلاع الموظف":"Employee Informed",
"رفض الاطلاع":"Declined to Acknowledge","اعتماد":"Approve","سبب رفض الاطلاع":"Reason for Declining Acknowledgement",
"ملاحظة اختيارية":"Optional Note","رفض":"Reject","مهم:":"Important:","لا توجد جزاءات أو مكافآت مسجلة.":"No sanctions or rewards have been recorded.",
"إدارة الرواتب":"Payroll Management","إنشاء مسير شهر جديد / Create payroll run":"Create a New Monthly Payroll Run",
"لا يمكن إنشاء المسير قبل اعتماد الحضور والانصراف للشهر نفسه بعد مراجعة ملف البرنامج وملف البصمة.":"A payroll run cannot be created until attendance for the same month has been approved after reviewing the program and fingerprint files.",
"السنة / Year":"Year","الشهر / Month":"Month","إنشاء مسير الرواتب / Create payroll":"Create Payroll Run",
"مسيرات الرواتب / Payroll runs":"Payroll Runs","تاريخ الصرف المخطط":"Planned Pay Date","تاريخ الإنشاء":"Created At",
"فتح المسير / Open":"Open Run","لا توجد مسيرات رواتب حتى الآن.":"No payroll runs yet.","كل المسيرات":"All Payroll Runs",
"موعد الصرف المخطط:":"Planned Pay Date:","مراحل اعتماد المسير / Payroll workflow":"Payroll Approval Workflow",
"المسودة ← اعتماد الموارد البشرية ← إغلاق الشهر ← تسجيل الصرف النقدي أو التحويل البنكي. إعادة فتح مسير مغلق تتطلب سببًا مسجلًا في سجل المراجعة.":"Draft → HR approval → Close month → Record cash or bank transfer payment. Reopening a closed run requires a reason recorded in the audit log.",
"اعتماد المسير":"Approve Payroll Run","إغلاق الشهر":"Close Month","سبب إعادة فتح المسير":"Reason for Reopening Payroll Run",
"إعادة فتح المسير":"Reopen Payroll Run","تفاصيل الرواتب / Employee payroll items":"Employee Payroll Details",
"الأساسي":"Base Salary","بعد التناسب":"Prorated Amount","إجمالي المستحقات":"Total Earnings",
"إجمالي الخصومات":"Total Deductions","الصافي":"Net Pay","المدفوع":"Paid","المتبقي":"Remaining","حالة الصرف":"Payment Status",
"غير مدفوع":"Unpaid","مدفوع جزئيًا":"Partially Paid","مدفوع بالكامل":"Fully Paid","مكافأة حضور":"Attendance Bonus",
"مكافأة أداء":"Performance Bonus","مكافأة إشغال":"Occupancy Bonus","ساعات إضافي":"Overtime Hours",
"مكافآت أخرى":"Other Bonuses","أيام غياب معتمدة":"Approved Absence Days","خصم تأخير/انصراف مبكر":"Late/Early Departure Deduction",
"حصة التأمين":"Insurance Contribution","الضرائب":"Taxes","خصومات أخرى":"Other Deductions","ملاحظات":"Notes",
"حفظ ومراجعة الصافي":"Save and Review Net Pay","اعتماد ساعات الإضافي":"Approve Overtime Hours",
"المبلغ المدفوع":"Payment Amount","تاريخ الصرف":"Payment Date","طريقة الصرف":"Payment Method","نقدي / Cash":"Cash",
"تحويل بنكي / Bank transfer":"Bank Transfer","مرجع / رقم إيصال":"Reference / Receipt Number","تسجيل دفعة":"Record Payment",
"لا توجد بنود رواتب في هذا المسير.":"No payroll items in this run.",
"تصدير تقرير Excel شامل / Export workbook":"Export Full Excel Report",
"اختر الفترة والموظف المطلوب يدويًا. سيحتوي الملف على أوراق منفصلة للموظفين، والحضور، والإجازات، وحركات الأرصدة، والجزاءات والمكافآت، والسلف، والرواتب، ومقارنة الأقسام.":"Manually select the date range and employee. The workbook contains separate sheets for employees, attendance, leave, balance transactions, sanctions and rewards, advances, payroll, and department comparisons.",
"تعذر إنشاء التقرير. راجع التاريخين وحاول مرة أخرى.":"Could not create the report. Check both dates and try again.",
"كل الموظفين / All employees":"All Employees","تنزيل ملف Excel / Download Excel":"Download Excel",
"تنزيل تقرير PDF / Download PDF":"Download PDF","تصدير تقرير PDF / Export PDF":"Export PDF Report",
"نسخة للطباعة بنفس الفترة وبنفس بيانات تقرير Excel، مع دعم الخط العربي.":"A print-ready version with the same date range and data as the Excel report.",
"محتويات التقرير":"Report Contents","بيانات الموظفين الحالية والمؤرشفة.":"Current and archived employee data.",
"الإجازات وحركات أرصدتها.":"Leave records and balance transactions.",
"الجزاءات والمكافآت والسلف وحالاتها.":"Sanctions, rewards, advances, and their statuses.",
"مسيرات الرواتب وحالة الصرف والمبالغ المدفوعة والمتبقية.":"Payroll runs, payment statuses, and paid and remaining amounts.",
"عدد الموظفين النشطين وإجمالي رواتبهم الأساسية حسب القسم.":"Active employee counts and total basic salaries by department.",
"النسخ الاحتياطي والاستعادة":"Backup & Restore","مكان حفظ النسخ / External backup location":"Backup Storage Location",
"يجب اختيار مجلد على قرص خارجي منفصل عن قرص تشغيل النظام. لا تحفظ النسخ على قرص C: إذا كان النظام مثبتًا عليه.":"Choose a folder on an external drive separate from the system drive. Do not store backups on drive C: if the application is installed there.",
"مسار القرص الخارجي / External drive folder":"External Drive Folder","حفظ المسار / Save location":"Save Location",
"المسار الحالي:":"Current Path:","لم يتم تحديد قرص النسخ الاحتياطي بعد.":"No backup drive has been selected.",
"إنشاء نسخة الآن / Create backup now":"Create Backup Now","إنشاء والتحقق / Create and verify":"Create and Verify",
"نوع النسخة":"Backup Type","يدوية / Manual":"Manual","يومية / Daily":"Daily","أسبوعية / Weekly":"Weekly","ربع سنوية / Quarterly":"Quarterly",
"سجل النسخ / Backup history":"Backup History","الحجم":"Size","المسار":"Path","التحقق":"Verification",
"سبب الاستعادة / Restore reason":"Restore Reason","اكتب RESTORE للتأكيد":"Type RESTORE to Confirm",
"استعادة هذه النسخة / Restore this backup":"Restore This Backup","لا توجد نسخ احتياطية مسجلة.":"No backups recorded.",
"تنبيه قبل الاستعادة":"Warning Before Restore","اكتب أمرًا أو سؤالًا":"Enter a Command or Question",
"السؤال العادي يُجاب عنه فقط. أي أمر تعديل يُحوَّل أولًا إلى خطة واضحة، ثم تختار أنت الموافقة أو الرفض قبل أي تغيير.":"Questions are answered without changes. A change request is first converted into a clear plan; you decide whether to approve or reject it.",
"الأمر أو السؤال":"Command or Question","إرسال":"Send","النتيجة":"Result","خطة تحتاج موافقتك":"Plan Requires Your Approval",
"هذه الخطة محفوظة ببصمة. صلاحيتها محدودة، ولا يمكن إعادة استخدام الموافقة بعد التنفيذ.":"This plan is fingerprinted and expires. Approval cannot be reused after execution.",
"موافقة وتنفيذ":"Approve and Execute","رفض العملية":"Reject Action","سياق المحادثة المحلي":"Local Conversation Context",
"أمثلة":"Examples","حدود الأمان":"Safety Boundaries","اسأل المساعد":"Ask the Assistant","سؤالك":"Your Question",
"تحليل السؤال":"Analyze Question","المساعد جاهز":"Assistant Ready","المساعد غير متاح":"Assistant Unavailable",
"الإجابة":"Answer","العودة للوحة التحكم":"Back to Dashboard","لا يتم إرسال البيانات إلى خدمة سحابية.":"Data is not sent to a cloud service.",
"المساعد لا يعتمد الرواتب أو الإجازات ولا يغيّر بيانات الموظفين.":"The assistant does not approve payroll or leave and does not change employee data.",
"أي إجراء مؤثر يحتاج مراجعة وتأكيد المسؤول داخل النظام.":"Any consequential action requires review and confirmation by the administrator in the system.",
"إذا كان النموذج غير متاح، تظل كل وظائف HR والرواتب الأساسية قابلة للاستخدام.":"All core HR and payroll functions remain available if the model is unavailable.",
"لا توجد بيانات":"No data available","لا توجد سجلات":"No records","الاسم":"Name","العدد":"Count",
"الراتب":"Salary","تاريخ العمل:":"Work Date:","الحالة:":"Status:","المدة:":"Duration:",
"المسؤول:":"Administrator:","صفحة":"Page","من":"of","تم الحفظ":"Saved","فشل":"Failed",
"تعذر تنفيذ العملية.":"The action could not be completed.","تعذر تنفيذ العملية":"The action could not be completed.",
"تم":"Done","تأكيد":"Confirm","حفظ":"Save","إضافة":"Add","تحديث":"Update","بحث":"Search",
"عرض":"View","تحميل":"Download","اختر":"Select","مطلوب":"Required","اختياري":"Optional",
"draft":"Draft","approved":"Approved","pending":"Pending","rejected":"Rejected","cancelled":"Cancelled",
"closed":"Closed","reopened":"Reopened","under_review":"Under Review","needs_review":"Needs Review",
"ready_for_review":"Ready for Review","success":"Successful","failed":"Failed","regular":"Regular",
"installment":"Installment","sanction":"Sanction","reward":"Reward","viewed":"Viewed",
"declined":"Declined","unpaid":"Unpaid","partially_paid":"Partially Paid","paid":"Paid",

"إدارة السلف": "Advance Management",
"تسجيل طلب سلفة / New advance request": "Submit an Advance Request",
"تُقبل الطلبات من يوم 15 إلى يوم 22 من الشهر فقط. السلفة الشهرية العادية فقط تخضع لحد 25% من الراتب الأساسي. السلفة المقسطة مستقلة عن هذا الحد ويمكن أن تتجاوز الراتب الشهري، ويحدد جدولها عدد الأقساط. شهر الاستحقاق المحاسبي هو شهر الطلب، ويُدفع الراتب في اليوم الخامس من الشهر التالي.": "Requests are accepted only from the 15th through the 22nd. Only regular monthly advances are subject to the 25% of basic salary limit. Installment advances are independent of this limit and may exceed monthly salary; the installment schedule determines the number of payments. The accounting month is the request month, and payroll is paid on the fifth day of the following month.",
"تاريخ الطلب / Request date": "Request Date",
"نوع السلفة / Advance type": "Advance Type",
"سلفة شهرية (تُخصم من راتب الشهر) — حد 25% من الأساسي": "Monthly Advance (deducted from the month's salary) — 25% of Basic Salary Limit",
"سلفة مقسطة — بدون حد 25%": "Installment Advance — No 25% Limit",
"تسجيل الطلب / Submit request": "Submit Request",
"ملخص السلف لكل موظف / Employee advance summary": "Advance Summary by Employee",
"آخر سلفة": "Most Recent Advance",
"إجمالي السلف التاريخي": "Historical Advance Total",
"المخصوم": "Deducted",
"المتبقي من السلفة الشهرية": "Monthly Advance Balance",
"قائم مقسط": "Outstanding Installment",
"الحد الأقصى 25% من الأساسي": "Max 25% of Basic Salary",
"المتاح للسلفة الشهرية": "Available for Monthly Advance",
"طلبات السلف والسجل / Advances register": "Advance Requests and Register",
"أساس الراتب": "Salary Basis",
"حد 25%": "25% Limit",
"خصم في راتب": "Payroll Deduction",
"الأقساط": "Installments",
"تأكيد الصرف": "Confirm Payment",
"لا توجد طلبات سلف مسجلة.": "No advance requests recorded.",
"ملاحظة:": "Note:",
"ملخص السلف يفصل بين الرصيد العادي والرصيد المقسط. الـ25% يطبق على السلفة الشهرية العادية فقط. السلفة المقسطة لا تخضع له، ويمكن أن تتجاوز الراتب، وخصمها يتم حسب جدول الأقساط. هذه الوحدة تسجل السلفة واعتمادها وصرفها وجدول الأقساط والشهر المستحق لكل خصم. الخصم الفعلي لا يُسجل كمخصوم إلا عند إغلاق دورة الرواتب، ويمكن عكسه عند إعادة فتح الدورة.": "The advance summary separates regular and installment balances. The 25% limit applies only to regular monthly advances. Installment advances are exempt and may exceed salary; deductions follow their installment schedule. This module records the advance, approval, payment, installment schedule, and accounting month for each deduction. A deduction is only marked as applied when payroll is closed and can be reversed if the payroll run is reopened.",
"تقارير الموارد البشرية · HR reports": "Human Resources Reports",
"اختر الفترة والموظف المطلوب يدويًا. سيحتوي الملف على أوراق منفصلة للموظفين، والحضور، والإجازات، وحركات الأرصدة، والجزاءات والمكافآت، والسلف، والرواتب، ومقارنة الأقسام.": "Select the date range and employee manually. The workbook contains separate sheets for employees, attendance, leave, balance transactions, sanctions and rewards, advances, payroll, and department comparisons.",
"تعذر إنشاء التقرير. راجع التاريخين وحاول مرة أخرى.": "Could not create the report. Check both dates and try again.",
"نسخة للطباعة بنفس الفترة وبنفس بيانات تقرير Excel، مع دعم الخط العربي.": "A print-ready copy with the same date range and data as the Excel report, including Arabic font support.",
"الحضور وحالات المراجعة كما هي مسجلة؛ لا يتم تحويل السجلات غير المحسومة إلى غياب.": "Attendance and review statuses are shown as recorded; unresolved records are not converted into absences.",
"التقرير قراءة فقط ولا يغيّر أي بيانات داخل النظام. التقرير بصيغة Excel يحتوي جميع الحقول، ونسخة PDF مناسبة للطباعة. التقرير يُنشأ يدويًا حسب الفترة والموظف المحددين ولا يتم تشغيله تلقائيًا.": "Reports are read-only and do not change system data. The Excel report contains all fields, and the PDF version is print-ready. Reports are generated manually for the selected period and employee and are never run automatically.",
"مساعد موارد بشرية للمسؤول فقط، يعمل محليًا. أي عملية تعديل تتطلب موافقتك الصريحة قبل التنفيذ.": "An HR assistant for the administrator only, running locally. Any change requires your explicit approval before execution.",
"السؤال العادي يُجاب عنه فقط. أي أمر تعديل يُحوَّل أولًا إلى خطة واضحة، ثم تختار أنت الموافقة أو الرفض قبل أي تغيير.": "Questions are answered without changes. A change request is first converted into a clear plan; you choose whether to approve or reject it before any change.",
"هذه الخطة محفوظة ببصمة. صلاحيتها محدودة، ولا يمكن إعادة استخدام الموافقة بعد التنفيذ.": "This plan is fingerprinted and expires. Approval cannot be reused after execution.",
"كم عدد الموظفين النشطين؟": "How many active employees are there?",
"اعتمد الإجازة رقم 12 — للمعاينة.": "Preview approval of leave request 12.",
"نفذ: اعتمد الإجازة رقم 12.": "Execute: approve leave request 12.",
"نفذ: اعتمد مسير الرواتب رقم 3.": "Execute: approve payroll run 3.",
"مساعد الموارد البشرية المحلي": "Local HR Assistant",
"مساعد تحليلي يعمل محليًا فقط ولا ينفذ أي تعديل تلقائي.": "An analytical assistant that runs locally and never makes automatic changes.",
"مثال: ما عدد الموظفين النشطين؟ ما الذي يحتاج مراجعة في الحضور؟": "Example: How many active employees are there? What attendance items need review?",
"ارفع ملف البرنامج أولًا باعتباره المصدر الأساسي، ثم ارفع ملف البصمة لاستكمال النواقص. يدعم النظام CSV وXLS وXLSX، ويتعامل مع عناوين الأعمدة العربية والإنجليزية، ويقرأ كل أوراق الملف وكل أوقات البصمة الموجودة في الخلية الواحدة.": "Upload the attendance program report first as the primary source, then upload the fingerprint file to fill gaps. CSV, XLS, and XLSX are supported. Arabic and English column headers are recognized, and all workbook sheets and fingerprint times in each cell are read.",
"الاستيراد يحفظ الأدلة الخام وملاحظات المطابقة فقط. لا يعتمد الحضور ولا يرسل بيانات إلى الرواتب.": "Import stores raw evidence and reconciliation notes only. It does not approve attendance or send data to payroll.",
"مراجعة مشكلات هذه الدفعة وحل هوية الموظف المجهول": "Review this batch's issues and resolve unknown employee identities",
"الانتقال إلى مطابقة الشهر": "Go to Monthly Reconciliation",
"تم تسجيل دفعة الاستيراد": "Import Batch Recorded",
"ملاحظات الملف": "File Issues",
"اختر الموظف الصحيح بعد مراجعة الاسم/الكود الأصلي": "Select the correct employee after checking the original name/code",
"هذه المشكلة لا يمكن حلها بتغيير هوية الموظف. راجع المصدر الأصلي وصحح الملف ثم أعد استيراده.": "This issue cannot be resolved by changing employee identity. Check the original source, correct the file, and import it again.",
"استورد تقرير البرنامج وملف البصمة للفترة نفسها لإنشاء سجل الشهر.": "Import the program report and fingerprint file for the same period to create the monthly record.",
"هذه فجوات تغطية تحتاج مراجعة التقرير الأساسي وملف البصمة. لا يصنفها النظام غيابًا أو راحة تلقائيًا.": "These coverage gaps require review of the primary report and fingerprint file. The system does not automatically classify them as absence or rest.",
"أؤكد أنني راجعت تغطية جميع الموظفين والأيام، وراجعت النواقص بين التقرير والبصمة، ولم أعتبر الخانات الفارغة غيابًا تلقائيًا.": "I confirm that I reviewed coverage for all employees and days, checked gaps between the report and fingerprint file, and did not treat blank cells as automatic absences.",
"اختر تقرير البرنامج باعتباره المصدر الأساسي، وملف البصمة لاستكمال النواقص. يمكن متابعة المطابقة حتى مع وجود ملاحظات استيراد؛ لكن وجود أي ملاحظة سيحوّل جميع المقترحات إلى «تحتاج مراجعة» حتى يراجعها مسؤول الموارد البشرية.": "Choose the program report as the primary source and the fingerprint file to fill gaps. Reconciliation can continue even when import issues exist; however, any issue marks all suggestions as requiring review until the HR administrator checks them.",
"يُنشئ النظام سجلات مقترحة لكل موظف وأيام الفترة، ويحدد الورديات الليلية بتاريخ بداية الوردية. أيام الأمن المعتمدة التي يظهر فيها الموظف OFF لا تُسجل كغياب.": "The system creates suggested records for each employee and day in the period, and assigns overnight shifts to the date on which the shift starts. Approved security roster days marked OFF are not recorded as absences.",
"لا يمكن إنشاء المسير قبل اعتماد الحضور والانصراف للشهر نفسه بعد مراجعة ملف البرنامج وملف البصمة.": "A payroll run cannot be created until attendance for the same month has been approved after reviewing the program and fingerprint files.",
"ملاحظات الحساب:": "Calculation Notes:",
"الأساسي الشهري ثابت، وساعات العمل لا تخفضه. سعر الساعة = الأساسي ÷ (ساعات اليوم × 30). خصم الغياب = الأساسي ÷ أيام الشهر الفعلية × أيام الغياب. الأوفر تايم بنفس سعر الساعة دون مضاعف، ولا يدخل في الصافي إلا بعد اعتماد ساعات الإضافي من الإدارة. التأمين والضرائب يدويان دون حساب قانوني تلقائي.": "Monthly base salary is fixed and is not reduced by hours worked. Hourly rate = basic salary ÷ (daily hours × 30). Absence deduction = basic salary ÷ actual days in the month × absence days. Overtime uses the same hourly rate without a multiplier and is included in net pay only after management approval. Insurance and taxes are entered manually without automatic legal calculations.",
"خصم السلف والمكافآت والجزاءات المالية يظهر من السجلات المعتمدة للشهر. لا تعتبر السلفة مخصومة إلا بعد إغلاق المسير.": "Advance deductions and financial rewards or sanctions are taken from approved records for the month. An advance is not considered deducted until the payroll run is closed.",
"يجب اختيار مجلد على قرص خارجي منفصل عن قرص تشغيل النظام. لا تحفظ النسخ على قرص C: إذا كان النظام مثبتًا عليه.": "Choose a folder on an external drive separate from the system drive. Do not store backups on drive C: if the application is installed there.",
"تحتوي النسخة على قاعدة بيانات PostgreSQL وصور الموظفين وملف تحقق ببصمات SHA-256. يتحقق النظام من سلامة ملف قاعدة البيانات قبل اعتبار النسخة ناجحة.": "The backup contains the PostgreSQL database, employee photos, and a SHA-256 integrity manifest. The system verifies the database file before marking the backup as successful.",
"جدول النسخ والاحتفاظ / Schedule and retention": "Backup Schedule and Retention",
"نسخة يومية: الاحتفاظ بها 40 يومًا.": "Daily backups: retained for 40 days.",
"نسخة أسبوعية: الاحتفاظ بها 365 يومًا.": "Weekly backups: retained for 365 days.",
"نسخة كاملة كل 3 أشهر: لا تُحذف تلقائيًا.": "A full backup every 3 months: never deleted automatically.",
"النسخ اليدوية ونسخ ما قبل الاستعادة لا تُحذف تلقائيًا.": "Manual backups and pre-restore backups are never deleted automatically.",
"المواعيد الافتراضية: يوميًا 02:00، الأحد 02:30، وأول يوم من كل ربع سنة 03:00. يمكن تعديلها من برنامج جدولة مهام Windows.": "Default schedule: daily at 02:00, Sundays at 02:30, and the first day of each quarter at 03:00. These can be changed in Windows Task Scheduler.",
"بعد تحديد مسار القرص الخارجي، يثبت مسؤول النظام مهام Windows مرة واحدة باستخدام ملف": "After selecting the external drive path, the system administrator installs the Windows tasks once using the file",
"الاستعادة تستبدل بيانات النظام الحالية ببيانات تاريخ النسخة المختارة. ينشئ النظام نسخة كاملة من الحالة الحالية أولًا، ويتحقق منها، ثم ينفذ الاستعادة. بعد نجاحها ستحتاج إلى تسجيل الدخول مرة أخرى باستخدام بيانات المسؤول الموجودة في النسخة المستعادة.": "Restoring replaces current system data with data from the selected backup date. The system first creates and verifies a full backup of the current state, then performs the restore. After a successful restore, sign in again using the administrator credentials contained in the restored backup.",

"تاريخ سريان التعديل": "Change Effective Date",
"سبب التعديل": "Change Reason",
"لا توجد تغييرات مسجلة.": "No changes recorded.",
"أرشفة": "Archive",
"إعادة تعيين": "Rehire",
"لا يوجد موظفون مسجلون بعد. أضف أول موظف من النموذج أعلاه.": "No employees have been registered yet. Add the first employee using the form above.",
"مراجعة": "Review",
"طلبات تصحيح البيانات الشخصية": "Personal Data Correction Requests",
"القيمة المطلوبة:": "Requested Value:",
"سبب القرار": "Decision Reason",
"تاريخ السريان (للموافقة)": "Effective Date (for Approval)",
"تطبيق التعديل": "Apply Change",
"رفض الطلب": "Reject Request",
"لا توجد طلبات تصحيح بيانات.": "No data correction requests.",
"الهيكل": "Organization Structure",
"الفروع الحالية": "Current Branches",
"إضافة القسم": "Add Department",
"حفظ القسم": "Save Department",
"إضافة فرع": "Add Branch",
"حفظ الفرع": "Save Branch",
"إدارة الإجازات والأرصدة": "Leave and Balance Management",
"يحسب النظام أيام الفترة التقويمية شاملًا يوم البداية والنهاية. أدخل أيام الخصم الفعلية يدويًا؛ لا يتم افتراض أيام الراحة الأسبوعية أو العطلات.": "The system counts calendar days including the start and end dates. Enter actual charged days manually; weekly rest days and holidays are not assumed.",
"الأرصدة الافتتاحية تدخل يدويًا حسب رصيد كل موظف. لا ينشئ النظام أي استحقاق تلقائي. التسوية الموجبة تزيد الرصيد والسالبة تخصمه.": "Opening balances are entered manually for each employee. The system creates no automatic accruals. Positive adjustments increase the balance; negative adjustments reduce it.",
"رصيد افتتاحي / Opening": "Opening Balance",
"تسوية / Adjustment": "Adjustment",
"يمكن إضافة نوع مخصص. تفعيل تتبع الرصيد يعني أن اعتماد الإجازة يخصم الأيام من رصيد الموظف.": "You can add a custom type. Enabling balance tracking means approved leave days are deducted from the employee's balance.",
"لا توجد حركات أرصدة بعد.": "No balance transactions yet.",
"المساعد التنفيذي":"AI Workspace",
"يفهم أوامرك، يجهّز العملية، ويعرض التفاصيل قبل أي تنفيذ. التنفيذ يتم فقط بعد تأكيدك الصريح.":"Understands your requests, prepares each operation, and shows the details before execution. Nothing is executed without your explicit confirmation.",
"النموذج:":"Model:",
"— الاتصال بالنموذج مقصور على الجهاز المحلي.":"— The model connection is restricted to this local device.",
"السؤال العادي يُجاب عنه مباشرة. أي أمر تعديل يُحوَّل إلى خطة واضحة ثم تختار أنت الموافقة أو الرفض.":"Questions are answered directly. Any change request is turned into a clear plan for your approval or rejection.",
"البيانات الحالية قبل التنفيذ":"Current data before execution",
"التغييرات المطلوبة بعد التنفيذ":"Proposed changes",
"الآثار والتنبيهات":"Effects and warnings",
"تفاصيل خطة التنفيذ":"Execution plan details",
"هذه الخطة محفوظة ببصمة. صلاحيتها محدودة، ولا يمكن إعادة استخدام الموافقة بعد التنفيذ.":"This plan is stored with a fingerprint and expires. Approval is single-use.",
"أمثلة سريعة":"Quick examples",
"اعتمد الإجازة رقم 12 — للمعاينة فقط.":"Approve leave request 12 — preview only.",
"أضف سلفة تقسيط للموظف MY-0004 بمبلغ 3000 على 6 أشهر.":"Add an installment advance of 3,000 for employee MY-0004 over 6 months.",
"مثال: نفذ: اعتمد مسير الرواتب رقم 3":"Example: Execute: approve payroll run 3",

}

ZH = {
"العربية":"阿拉伯语","English":"英语","中文":"中文","ماذا يستطيع المساعد؟":"助手能做什么？","يسأل ويجيب":"问答","أرقام وحالات مباشرة من قاعدة البيانات: الموظفون، الحضور، الإجازات، الرواتب.":"直接来自数据库的数据和状态：员工、考勤、休假、工资。","ينفذ بأمان":"安全执行","أوامر التعديل تتحول إلى خطة واضحة معروضة عليك، ولا يتم أي تنفيذ قبل موافقتك الصريحة.":"修改指令会变成清晰展示的计划，未经您明确同意不会执行。","خصوصية كاملة":"完全隐私","كل المعالجة تتم على جهازك المحلي، ولا تغادر بيانات الفندق إلى أي خدمة خارجية.":"所有处理都在本地设备完成，酒店数据不会发送到任何外部服务。","اضغط على أي مثال لنسخه في مربع السؤال.":"点击任意示例即可复制到问题框。","ما الذي يحتاج مراجعة في الحضور هذا الشهر؟":"本月考勤有哪些需要复核？","نظام الموارد البشرية والرواتب":"人力资源与薪资管理系统",
"الرئيسية":"首页","لوحة التحكم":"控制面板","مساعد الذكاء الاصطناعي":"AI 助手","الموارد البشرية":"人力资源",
"الموظفون":"员工","الحضور والانصراف":"考勤","الإجازات":"休假","الجزاءات والمكافآت":"奖惩管理",
"جدول الأمن":"安保排班","الرواتب والمالية":"薪资与财务","مسيرات الرواتب":"工资批次","السلف":"预支工资",
"التقارير":"报表","النظام":"系统","النسخ الاحتياطي":"备份",
"تسجيل الدخول — نظام الموارد البشرية":"登录 — 人力资源系统","تسجيل الدخول":"登录","شعار الفندق":"酒店标志",
"اسم المستخدم":"用户名","أدخل اسم المستخدم":"请输入用户名","كلمة المرور":"密码","أدخل كلمة المرور":"请输入密码",
"دخول":"登录","تعليمات":"说明","يعمل محليًا على Windows 10 و 11 — ولا يُعرض على الإنترنت.":"在 Windows 10 和 11 本地运行，不对互联网开放。",
"اسم المستخدم أو كلمة المرور غير صحيحة.":"用户名或密码不正确。",
"نظرة سريعة على حالة العمل والمهام التي تحتاج قرار مسؤول الموارد البشرية.":"快速查看运营情况以及需要人力资源管理员处理的事项。",
"مساعد AI المحلي":"本地 AI 助手","تسجيل الخروج":"退出登录","الحالة الآن":"当前状态","أهلًا بك،":"欢迎，",
"كل الأرقام التالية مأخوذة مباشرة من قاعدة البيانات المحلية.":"以下数据均直接来自本地数据库。","فتح التقارير":"打开报表",
"مؤشرات رئيسية":"关键指标","ملخص النظام":"系统摘要","الموظفون النشطون":"在职员工",
"إدارة الموظفين ←":"管理员工 ←","الموظفون السابقون":"离职员工","السجل الوظيفي ←":"员工档案 ←",
"حضور يحتاج مراجعة":"待审核考勤","مراجعة الحضور ←":"审核考勤 ←","إجازات غير مؤكدة":"待确认休假",
"فتح الإجازات ←":"查看休假 ←","سلف قيد الإجراء":"处理中预支","فتح السلف ←":"查看预支 ←",
"جزاءات ومكافآت للمراجعة":"待审核奖惩","فتح الإجراءات ←":"查看操作 ←","مسيرات رواتب مفتوحة":"未关闭工资批次",
"فتح الرواتب ←":"查看薪资 ←","المهام":"任务","ما يحتاج متابعة":"需要跟进的事项",
"استيراد الحضور والبصمة":"导入考勤与指纹记录","رفع الملفات الأصلية ومراجعة مشاكل الاستيراد.":"上传源文件并检查导入问题。",
"مطابقة الحضور الشهرية":"月度考勤核对","مراجعة الفجوات والاستثناءات قبل الاعتماد.":"批准前检查缺失记录和异常情况。",
"مراجعة الرواتب":"审核薪资","معاينة المسير قبل الاعتماد والإغلاق.":"批准和关闭前预览工资批次。",
"حماية البيانات والتحقق من النسخ.":"保护数据并验证备份。","الوحدات":"功能模块","الوصول السريع":"快速访问",
"البيانات والهيكل الإداري":"员工资料与组织架构","الاستيراد والمراجعة والمطابقة":"导入、审核与核对",
"الأرصدة والطلبات والسجل":"余额、申请与记录","المعاينة والاعتماد والصرف":"预览、批准与付款",
"الطلبات والأقساط والخصومات":"申请、分期与扣款","التسجيل والاعتماد والتاريخ":"记录、批准与历史",
"Excel وPDF وفترات مخصصة":"Excel、PDF 与自定义日期范围","الاستيراد والتحقق اليومي":"导入与每日验证",
"ذكاء محلي اختياري":"可选本地 AI","مساعد AI للموارد البشرية":"人力资源 AI 助手",
"تحليل الأسئلة والبيانات بشكل محلي، مع مراجعة المسؤول قبل أي إجراء مؤثر.":"在本地分析问题和数据；任何重要操作都必须由管理员审核。",
"فتح المساعد":"打开助手","إدارة الموظفين":"员工管理",
"عرض بطاقات منظم: بيانات واضحة وإجراءات مرتبة دون جداول طويلة.":"使用清晰的员工卡片展示资料和操作，避免冗长表格。",
"تسجيل جديد":"新建记录","إضافة موظف":"添加员工","الاسم بالكامل":"姓名","الرقم القومي":"身份证号码","الجنسية":"国籍","مصري":"埃及籍","أجنبي":"外籍","رقم الباسبور (للأجانب)":"护照号码（外籍）",
"رقم الهاتف":"电话号码","العنوان":"地址","القسم":"部门","الفرع":"分支机构","بدون فرع":"无分支机构",
"المسمى الوظيفي":"职位","تاريخ التعيين":"入职日期","بداية فترة الاختبار":"试用期开始日期","نهاية فترة الاختبار":"试用期结束日期",
"الراتب الأساسي بالجنيه":"基本工资（埃及镑）","ساعات العمل اليومية":"每日工作小时数","رقم البصمة":"指纹设备编号",
"الصورة الشخصية (اختياري JPG/PNG/WEBP، حد 5MB)":"个人照片（可选 JPG/PNG/WEBP，最大 5 MB）",
"حفظ الموظف":"保存员工","السجل":"记录","بطاقات الموظفين":"员工卡片","نشط":"在职","الراتب الأساسي":"基本工资",
"ج.م":"埃及镑","ساعة":"小时","ساعات يومية":"每日工时","تعديل البيانات":"编辑资料","حفظ التعديلات":"保存更改",
"نقل قسم / فرع":"调动部门 / 分支机构","القسم الجديد":"新部门","الفرع الجديد":"新分支机构","تاريخ سريان النقل":"调动生效日期",
"سبب النقل":"调动原因","تسجيل النقل":"记录调动","طلب تصحيح بيانات":"申请更正资料","البيان":"字段",
"القيمة المطلوبة":"申请更正的内容","سبب الطلب":"申请原因","تسجيل الطلب":"提交申请","سجل التغييرات":"变更记录",
"اسم القسم":"部门名称","إضافة قسم":"添加部门","الأقسام الحالية":"现有部门","إضافة الفرع":"添加分支机构",
"الأقسام والفروع":"部门与分支机构","اسم الفرع":"分支机构名称","لا يوجد موظفون حاليون.":"目前没有在职员工。",
"تسجيل إجازة جديدة / New leave record":"新建休假记录","نوع الإجازة / Leave type":"休假类型","الموظف / Employee":"员工",
"من تاريخ / Start date":"开始日期","إلى تاريخ / End date":"结束日期","أيام الخصم من الرصيد / Charged days":"余额扣减天数",
"السبب / Reason":"原因","حفظ كمسودة / Save draft":"保存为草稿","تسجيل رصيد افتتاحي أو تسوية / Balance entry":"期初余额或调整",
"نوع الرصيد":"余额类型","نوع الحركة":"交易类型","عدد الأيام":"天数","تسجيل الحركة / Record balance":"记录交易",
"أرصدة الموظفين الحالية / Current balances":"员工当前余额","سجل الإجازات / Leave history":"休假记录","النوع":"类型",
"الفترة":"期间","أيام تقويمية":"日历天数","أيام خصم":"扣减天数","الحالة":"状态","الإجراء":"操作",
"اعتماد وخصم الرصيد":"批准并扣减余额","سبب الإلغاء":"取消原因","إلغاء":"取消","لا توجد إجازات مسجلة بعد.":"尚无休假记录。",
"أنواع الإجازات / Leave types":"休假类型","الكود / Code":"代码","الاسم بالعربية":"阿拉伯语名称",
"الاسم بالإنجليزية":"英语名称","مدفوعة / Paid":"带薪","تخصم من رصيد / Tracks balance":"扣减余额",
"إضافة نوع / Add type":"添加休假类型","آخر حركات الأرصدة / Recent balance transactions":"最近余额交易",
"التاريخ":"日期","الحركة":"交易","الأيام":"天","لا توجد حركات أرصدة بعد.":"尚无余额交易。",
"مراجعة واعتماد الحضور":"考勤审核与批准","إضافة حضور يدوي":"添加手动考勤","الموظف":"员工","اختر الموظف":"选择员工",
"تاريخ العمل":"工作日期","وقت الدخول":"上班时间","وقت الخروج":"下班时间","سبب الإدخال اليدوي":"手动录入原因",
"إضافة الحضور للمراجعة":"添加考勤以供审核","لا توجد سجلات معلقة":"没有待处理记录","مطابقة شهر جديد":"核对新月份",
"اعتماد الشهور":"批准月份","السابق":"上一页","التالي":"下一页","الوردية:":"班次：","غير محددة":"未指定",
"غير موجود":"不可用","غير موجودة":"不可用","غير مسجل":"未记录","غير معروف":"未知","غير محسوبة":"未计算",
"تقرير البرنامج:":"系统报表：","البصمة:":"指纹记录：","إدخال HR اليدوي:":"人力资源手动录入：",
"سبب الإدخال:":"录入原因：","أدخله:":"录入人：","وقت التسجيل:":"记录时间：","الدقائق المقترحة:":"建议分钟数：",
"ملاحظات:":"备注：","وقت الدخول المعتمد بعد المراجعة":"审核后的批准上班时间","وقت الخروج المعتمد بعد المراجعة":"审核后的批准下班时间",
"سبب المراجعة والاعتماد":"审核与批准原因","اعتماد هذا اليوم":"批准此日期","مراجعة واعتماد شهور الحضور":"考勤月份审核与批准",
"مراجعة الأيام":"审核日期","لا توجد شهور مستوردة":"没有已导入月份","بداية الفترة":"开始日期","نهاية الفترة":"结束日期",
"سبب إغلاق الشهر":"月份关闭原因","اعتماد الشهر نهائيًا":"最终批准月份","تم اعتماد الشهر بواسطة:":"月份批准人：",
"استيراد جدول الأمن":"导入安保排班","رفع ملف Excel":"上传 Excel 文件","رفع ومعاينة":"上传并预览",
"ملاحظات الاستيراد":"导入问题","كل يوم يُراجع ويُعتمد منفصلًا.":"每天分别审核和批准。",
"تم اعتماد هذا اليوم بالفعل":"此日期已批准","جاهز للاعتماد بعد المراجعة":"审核后即可批准",
"يحتاج إلى مراجعة وتصحيح":"需要审核和更正","البداية":"开始","النهاية":"结束","المدة":"时长",
"راجعت ورديات هذا اليوم":"我已审核当天班次","اعتماد اليوم":"批准日期","لن يتم حفظ تعيينات هذا اليوم.":"此日期的排班不会保存。",
"استيراد الحضور والانصراف":"导入考勤","رفع تقرير الحضور أو البصمة":"上传考勤或指纹报表","نوع الملف":"文件类型",
"تقرير برنامج الحضور (المصدر الأساسي)":"考勤系统报表（主要来源）","تقرير جهاز البصمة (استكمال النواقص)":"指纹设备报表（补充缺失数据）",
"ملف الحضور / Attendance file":"考勤文件","استيراد وحفظ الأدلة للمراجعة":"导入并保存证据以供审核",
"الحد الأقصى لحجم الملف 5 ميجابايت. لا يتم تخمين هوية موظف غير معروف.":"文件最大为 5 MB。系统不会猜测未知员工身份。",
"تعذر الاستيراد:":"导入失败：","تم تسجيل دفعة الاستيراد":"导入批次已记录","رقم الدفعة:":"批次编号：",
"تحميل نسخة الملف الأصلي المحفوظة":"下载已保存的原始文件","عدد أحداث الدخول/الخروج المستخرجة:":"提取的上下班事件数：",
"عدد الملاحظات التي تحتاج مراجعة:":"待审核问题数：","تم الاستيراد وتنتظر المطابقة":"已导入 — 等待核对",
"مراجعة مشكلات استيراد الحضور":"审核考勤导入问题","العودة للاستيراد":"返回导入","تعذر تنفيذ الإجراء:":"操作失败：",
"الملف:":"文件：","الملاحظات غير المحسومة:":"未解决问题：","تحميل الملف الأصلي":"下载原始文件","المشكلة رقم":"问题编号",
"الورقة:":"工作表：","الصف:":"行：","القيمة الأصلية:":"原始值：","غير محددة":"未指定","غير محدد":"未指定",
"غير متاحة":"不可用","تم الحل:":"已解决：","بواسطة":"由","في":"于",
"اختر الموظف الصحيح بعد مراجعة الاسم/الكود الأصلي":"核对原始姓名/代码后选择正确员工",
"تأكيد ربط البصمات بهذا الموظف وتسجيل المراجعة":"确认将指纹记录关联到该员工并记录审核",
"لا توجد مشكلات مسجلة في هذه الدفعة.":"此批次没有记录问题。",
"مطابقة الحضور الشهرية":"月度考勤核对","إنشاء مسودة مراجعة الشهر":"创建月度审核草稿","دفعة تقرير البرنامج":"考勤系统报表批次",
"دفعة جهاز البصمة":"指纹设备批次","اختر ملف البرنامج":"选择系统报表","اختر ملف البصمة":"选择指纹文件",
"إنشاء مسودة المطابقة":"创建核对草稿","تعذر إنشاء المطابقة:":"无法创建核对：","تم إنشاء مسودة المراجعة":"审核草稿已创建",
"الفترة:":"期间：","إجمالي سجلات الأيام:":"每日记录总数：","جاهزة لمراجعة الموارد البشرية:":"可供人力资源审核：",
"تحتاج مراجعة أو استكمال:":"需要审核或补充：","ملاحظات الاستيراد في الملفين:":"两个文件中的导入问题：",
"لم يتم اعتماد أي يوم أو استخدامه في الرواتب.":"没有任何日期获批或用于薪资计算。","فتح قائمة مراجعة الحضور":"打开考勤审核列表",
"تسجيل جزاء أو مكافأة / New staff action":"记录奖惩","نوع الإجراء / Type":"操作类型","جزاء / Sanction":"处罚",
"مكافأة / Reward":"奖励","التصنيف / Category":"类别","الجزاءات / Sanctions":"处罚","تنبيه شفهي / Verbal notice":"口头提醒",
"إنذار أول / First warning":"第一次警告","إنذار نهائي / Final warning":"最终警告","خصم من الراتب / Salary deduction":"工资扣款",
"إيقاف عن العمل / Suspension":"停职","المكافآت / Rewards":"奖励","مكافأة مالية / Financial reward":"现金奖励",
"شهادة تقدير / Certificate":"荣誉证书","إشادة / Commendation":"表扬","توصية بالترقية / Promotion recommendation":"晋升建议",
"نوع مخصص / Custom":"自定义","عنوان الإجراء / Title":"操作标题","شهر الرواتب / Payroll month":"薪资月份",
"المبلغ بالجنيه / Amount (EGP)":"金额（埃及镑）","إجراء مالي / Financial item":"财务项目","السبب والتفاصيل / Reason":"原因与详情",
"حفظ كمسودة / Save draft":"保存为草稿","سجل الجزاءات والمكافآت / History":"奖惩记录","القيمة":"金额",
"شهر الراتب":"薪资月份","إفادة الموظف":"员工确认","الإجراءات":"操作","تم إطلاع الموظف":"已告知员工",
"رفض الاطلاع":"拒绝确认","اعتماد":"批准","سبب رفض الاطلاع":"拒绝确认的原因","ملاحظة اختيارية":"可选备注","رفض":"拒绝",
"مهم:":"重要：","لا توجد جزاءات أو مكافآت مسجلة.":"尚无奖惩记录。",
"إدارة الرواتب":"薪资管理","إنشاء مسير شهر جديد / Create payroll run":"创建新的月度工资批次",
"السنة / Year":"年份","الشهر / Month":"月份","إنشاء مسير الرواتب / Create payroll":"创建工资批次",
"مسيرات الرواتب / Payroll runs":"工资批次","تاريخ الصرف المخطط":"计划发薪日期","تاريخ الإنشاء":"创建日期",
"فتح المسير / Open":"打开批次","لا توجد مسيرات رواتب حتى الآن.":"尚无工资批次。","كل المسيرات":"所有批次",
"موعد الصرف المخطط:":"计划发薪日期：","مراحل اعتماد المسير / Payroll workflow":"工资审批流程",
"اعتماد المسير":"批准工资批次","إغلاق الشهر":"关闭月份","سبب إعادة فتح المسير":"重新打开工资批次的原因",
"إعادة فتح المسير":"重新打开工资批次","تفاصيل الرواتب / Employee payroll items":"员工薪资明细",
"الأساسي":"基本工资","بعد التناسب":"按比例折算","إجمالي المستحقات":"收入总额","إجمالي الخصومات":"扣款总额",
"الصافي":"实发工资","المدفوع":"已支付","المتبقي":"剩余","حالة الصرف":"支付状态","غير مدفوع":"未支付",
"مدفوع جزئيًا":"部分支付","مدفوع بالكامل":"已全额支付","مكافأة حضور":"出勤奖金","مكافأة أداء":"绩效奖金",
"مكافأة إشغال":"入住率奖金","ساعات إضافي":"加班小时","مكافآت أخرى":"其他奖金","أيام غياب معتمدة":"已批准缺勤天数",
"خصم تأخير/انصراف مبكر":"迟到/早退扣款","حصة التأمين":"保险缴款","الضرائب":"税款","خصومات أخرى":"其他扣款",
"ملاحظات":"备注","حفظ ومراجعة الصافي":"保存并审核实发工资","اعتماد ساعات الإضافي":"批准加班时间",
"المبلغ المدفوع":"支付金额","تاريخ الصرف":"支付日期","طريقة الصرف":"支付方式","نقدي / Cash":"现金",
"تحويل بنكي / Bank transfer":"银行转账","مرجع / رقم إيصال":"参考编号 / 收据号码","تسجيل دفعة":"记录付款",
"لا توجد بنود رواتب في هذا المسير.":"此工资批次中没有薪资项目。",
"تصدير تقرير Excel شامل / Export workbook":"导出完整 Excel 报表","كل الموظفين / All employees":"所有员工",
"تنزيل ملف Excel / Download Excel":"下载 Excel","تنزيل تقرير PDF / Download PDF":"下载 PDF",
"تصدير تقرير PDF / Export PDF":"导出 PDF 报表","محتويات التقرير":"报表内容",
"بيانات الموظفين الحالية والمؤرشفة.":"在职和已归档员工资料。","الإجازات وحركات أرصدتها.":"休假记录及余额交易。",
"الجزاءات والمكافآت والسلف وحالاتها.":"处罚、奖励、预支工资及其状态。",
"مسيرات الرواتب وحالة الصرف والمبالغ المدفوعة والمتبقية.":"工资批次、付款状态、已付金额和剩余金额。",
"عدد الموظفين النشطين وإجمالي رواتبهم الأساسية حسب القسم.":"按部门统计在职员工人数和基本工资总额。",
"النسخ الاحتياطي والاستعادة":"备份与恢复","مكان حفظ النسخ / External backup location":"备份存储位置",
"مسار القرص الخارجي / External drive folder":"外部磁盘文件夹","حفظ المسار / Save location":"保存位置",
"المسار الحالي:":"当前路径：","لم يتم تحديد قرص النسخ الاحتياطي بعد.":"尚未选择备份磁盘。",
"إنشاء نسخة الآن / Create backup now":"立即创建备份","إنشاء والتحقق / Create and verify":"创建并验证",
"نوع النسخة":"备份类型","يدوية / Manual":"手动","يومية / Daily":"每日","أسبوعية / Weekly":"每周",
"ربع سنوية / Quarterly":"每季度","سجل النسخ / Backup history":"备份历史","الحجم":"大小","المسار":"路径",
"التحقق":"验证","سبب الاستعادة / Restore reason":"恢复原因","اكتب RESTORE للتأكيد":"输入 RESTORE 以确认",
"استعادة هذه النسخة / Restore this backup":"恢复此备份","لا توجد نسخ احتياطية مسجلة.":"没有备份记录。",
"تنبيه قبل الاستعادة":"恢复前警告","مساعد AI المحلي":"本地 AI 助手","اكتب أمرًا أو سؤالًا":"输入指令或问题",
"الأمر أو السؤال":"指令或问题","إرسال":"发送","النتيجة":"结果","خطة تحتاج موافقتك":"需要您批准的计划",
"موافقة وتنفيذ":"批准并执行","رفض العملية":"拒绝操作","سياق المحادثة المحلي":"本地对话上下文",
"أمثلة":"示例","حدود الأمان":"安全限制","اسأل المساعد":"询问助手","سؤالك":"您的问题",
"تحليل السؤال":"分析问题","المساعد جاهز":"助手已就绪","المساعد غير متاح":"助手不可用",
"الإجابة":"回答","العودة للوحة التحكم":"返回控制面板","لا يتم إرسال البيانات إلى خدمة سحابية.":"数据不会发送到云服务。",
"المساعد لا يعتمد الرواتب أو الإجازات ولا يغيّر بيانات الموظفين.":"助手不会批准工资或休假，也不会更改员工资料。",
"أي إجراء مؤثر يحتاج مراجعة وتأكيد المسؤول داخل النظام.":"任何重要操作都需要管理员在系统中审核并确认。",
"إذا كان النموذج غير متاح، تظل كل وظائف HR والرواتب الأساسية قابلة للاستخدام.":"即使模型不可用，所有核心人力资源和薪资功能仍可使用。",
"draft":"草稿","approved":"已批准","pending":"待处理","rejected":"已拒绝","cancelled":"已取消",
"closed":"已关闭","reopened":"已重新打开","under_review":"审核中","needs_review":"需要审核",
"ready_for_review":"待审核","success":"成功","failed":"失败","regular":"普通","installment":"分期",
"sanction":"处罚","reward":"奖励","viewed":"已告知","declined":"拒绝确认","unpaid":"未支付",
"partially_paid":"部分支付","paid":"已支付",
"إدارة السلف": "预支工资管理",
"تسجيل طلب سلفة / New advance request": "提交预支工资申请",
"تُقبل الطلبات من يوم 15 إلى يوم 22 من الشهر فقط. السلفة الشهرية العادية فقط تخضع لحد 25% من الراتب الأساسي. السلفة المقسطة مستقلة عن هذا الحد ويمكن أن تتجاوز الراتب الشهري، ويحدد جدولها عدد الأقساط. شهر الاستحقاق المحاسبي هو شهر الطلب، ويُدفع الراتب في اليوم الخامس من الشهر التالي.": "申请仅在每月15日至22日受理。只有普通月度预支受基本工资25%的上限限制。分期预支不受此限制，金额可以超过月薪；分期计划决定还款期数。会计归属月份为申请月份，工资于次月5日发放。",
"تاريخ الطلب / Request date": "申请日期",
"نوع السلفة / Advance type": "预支类型",
"سلفة شهرية (تُخصم من راتب الشهر) — حد 25% من الأساسي": "月度预支（从当月工资扣除）— 基本工资的25%上限",
"سلفة مقسطة — بدون حد 25%": "分期预支 — 不受25%上限限制",
"تسجيل الطلب / Submit request": "提交申请",
"ملخص السلف لكل موظف / Employee advance summary": "员工预支汇总",
"آخر سلفة": "最近一次预支",
"إجمالي السلف التاريخي": "历史预支总额",
"المخصوم": "已扣除",
"المتبقي من السلفة الشهرية": "月度预支余额",
"قائم مقسط": "分期未偿余额",
"الحد الأقصى 25% من الأساسي": "基本工资25%上限",
"المتاح للسلفة الشهرية": "月度预支可用额度",
"طلبات السلف والسجل / Advances register": "预支申请与登记",
"أساس الراتب": "工资基数",
"حد 25%": "25%上限",
"خصم في راتب": "工资扣款",
"الأقساط": "分期",
"تأكيد الصرف": "确认发放",
"لا توجد طلبات سلف مسجلة.": "没有预支申请记录。",
"ملخص السلف يفصل بين الرصيد العادي والرصيد المقسط. الـ25% يطبق على السلفة الشهرية العادية فقط. السلفة المقسطة لا تخضع له، ويمكن أن تتجاوز الراتب، وخصمها يتم حسب جدول الأقساط. هذه الوحدة تسجل السلفة واعتمادها وصرفها وجدول الأقساط والشهر المستحق لكل خصم. الخصم الفعلي لا يُسجل كمخصوم إلا عند إغلاق دورة الرواتب، ويمكن عكسه عند إعادة فتح الدورة.": "预支汇总区分普通余额和分期余额。25%上限仅适用于普通月度预支。分期预支不受此限制，金额可超过工资，并按分期计划扣款。本模块记录申请、批准、发放、分期计划以及每笔扣款的会计月份。只有工资批次关闭后，扣款才标记为已扣除；重新打开批次时可以撤销。",
"تقارير الموارد البشرية · HR reports": "人力资源报表",
"اختر الفترة والموظف المطلوب يدويًا. سيحتوي الملف على أوراق منفصلة للموظفين، والحضور، والإجازات، وحركات الأرصدة، والجزاءات والمكافآت، والسلف، والرواتب، ومقارنة الأقسام.": "手动选择日期范围和员工。工作簿包含员工、考勤、休假、余额交易、奖惩、预支工资、薪资和部门比较等独立工作表。",
"تعذر إنشاء التقرير. راجع التاريخين وحاول مرة أخرى.": "无法生成报表。请检查日期后重试。",
"نسخة للطباعة بنفس الفترة وبنفس بيانات تقرير Excel، مع دعم الخط العربي.": "可打印版本，日期范围和数据与 Excel 报表相同，并支持阿拉伯字体。",
"الحضور وحالات المراجعة كما هي مسجلة؛ لا يتم تحويل السجلات غير المحسومة إلى غياب.": "考勤和审核状态按记录显示；未解决的记录不会自动转换为缺勤。",
"التقرير قراءة فقط ولا يغيّر أي بيانات داخل النظام. التقرير بصيغة Excel يحتوي جميع الحقول، ونسخة PDF مناسبة للطباعة. التقرير يُنشأ يدويًا حسب الفترة والموظف المحددين ولا يتم تشغيله تلقائيًا.": "报表为只读，不会更改系统数据。Excel 报表包含所有字段，PDF 版本适合打印。报表根据所选日期范围和员工手动生成，不会自动运行。",
"مساعد موارد بشرية للمسؤول فقط، يعمل محليًا. أي عملية تعديل تتطلب موافقتك الصريحة قبل التنفيذ.": "仅供管理员使用的人力资源助手，在本地运行。任何更改都必须在执行前获得明确批准。",
"السؤال العادي يُجاب عنه فقط. أي أمر تعديل يُحوَّل أولًا إلى خطة واضحة، ثم تختار أنت الموافقة أو الرفض قبل أي تغيير.": "普通问题只会得到回答。任何修改指令都会先转换为清晰计划，之后由您决定批准或拒绝。",
"هذه الخطة محفوظة ببصمة. صلاحيتها محدودة، ولا يمكن إعادة استخدام الموافقة بعد التنفيذ.": "此计划带有指纹校验且有效期有限。执行后不能重复使用批准。",
"كم عدد الموظفين النشطين؟": "目前有多少名在职员工？",
"اعتمد الإجازة رقم 12 — للمعاينة.": "预览批准休假申请12。",
"نفذ: اعتمد الإجازة رقم 12.": "执行：批准休假申请12。",
"نفذ: اعتمد مسير الرواتب رقم 3.": "执行：批准工资批次3。",
"مساعد الموارد البشرية المحلي": "本地人力资源助手",
"مساعد تحليلي يعمل محليًا فقط ولا ينفذ أي تعديل تلقائي.": "仅在本地运行的分析助手，不会自动执行任何更改。",
"مثال: ما عدد الموظفين النشطين؟ ما الذي يحتاج مراجعة في الحضور؟": "示例：目前有多少名在职员工？哪些考勤事项需要审核？",
"ارفع ملف البرنامج أولًا باعتباره المصدر الأساسي، ثم ارفع ملف البصمة لاستكمال النواقص. يدعم النظام CSV وXLS وXLSX، ويتعامل مع عناوين الأعمدة العربية والإنجليزية، ويقرأ كل أوراق الملف وكل أوقات البصمة الموجودة في الخلية الواحدة.": "请先上传考勤系统报表作为主要来源，再上传指纹文件以补充缺失数据。支持 CSV、XLS 和 XLSX，识别阿拉伯语和英语列标题，并读取工作簿中的所有工作表及单元格中的全部指纹时间。",
"الاستيراد يحفظ الأدلة الخام وملاحظات المطابقة فقط. لا يعتمد الحضور ولا يرسل بيانات إلى الرواتب.": "导入仅保存原始证据和核对备注，不会批准考勤，也不会将数据发送至薪资模块。",
"مراجعة مشكلات هذه الدفعة وحل هوية الموظف المجهول": "审核此批次问题并确认未知员工身份",
"الانتقال إلى مطابقة الشهر": "前往月度核对",
"ملاحظات الملف": "文件问题",
"هذه المشكلة لا يمكن حلها بتغيير هوية الموظف. راجع المصدر الأصلي وصحح الملف ثم أعد استيراده.": "此问题不能通过更改员工身份解决。请检查原始来源、修正文件并重新导入。",
"استورد تقرير البرنامج وملف البصمة للفترة نفسها لإنشاء سجل الشهر.": "导入同一期间的考勤系统报表和指纹文件，以创建月度记录。",
"هذه فجوات تغطية تحتاج مراجعة التقرير الأساسي وملف البصمة. لا يصنفها النظام غيابًا أو راحة تلقائيًا.": "这些考勤缺口需要检查主要报表和指纹文件。系统不会自动将其分类为缺勤或休息。",
"أؤكد أنني راجعت تغطية جميع الموظفين والأيام، وراجعت النواقص بين التقرير والبصمة، ولم أعتبر الخانات الفارغة غيابًا تلقائيًا.": "我确认已检查所有员工和日期的考勤覆盖情况，核对报表与指纹记录中的缺失项，且未将空白单元格自动视为缺勤。",
"اختر تقرير البرنامج باعتباره المصدر الأساسي، وملف البصمة لاستكمال النواقص. يمكن متابعة المطابقة حتى مع وجود ملاحظات استيراد؛ لكن وجود أي ملاحظة سيحوّل جميع المقترحات إلى «تحتاج مراجعة» حتى يراجعها مسؤول الموارد البشرية.": "请选择考勤系统报表作为主要来源，并使用指纹文件补充缺失数据。即使存在导入问题，也可以继续核对；但任何问题都会使所有建议标记为“需要审核”，直到人力资源管理员完成检查。",
"يُنشئ النظام سجلات مقترحة لكل موظف وأيام الفترة، ويحدد الورديات الليلية بتاريخ بداية الوردية. أيام الأمن المعتمدة التي يظهر فيها الموظف OFF لا تُسجل كغياب.": "系统会为期间内每位员工和每天创建建议记录，并按夜班开始日期归属夜班。已批准的安保排班中标记为 OFF 的日期不会记为缺勤。",
"لا يمكن إنشاء المسير قبل اعتماد الحضور والانصراف للشهر نفسه بعد مراجعة ملف البرنامج وملف البصمة.": "审核考勤系统报表和指纹文件并批准同月考勤后，才能创建工资批次。",
"ملاحظات الحساب:": "计算说明：",
"الأساسي الشهري ثابت، وساعات العمل لا تخفضه. سعر الساعة = الأساسي ÷ (ساعات اليوم × 30). خصم الغياب = الأساسي ÷ أيام الشهر الفعلية × أيام الغياب. الأوفر تايم بنفس سعر الساعة دون مضاعف، ولا يدخل في الصافي إلا بعد اعتماد ساعات الإضافي من الإدارة. التأمين والضرائب يدويان دون حساب قانوني تلقائي.": "月基本工资固定，不因实际工时减少。小时工资 = 基本工资 ÷（每日工时 × 30）。缺勤扣款 = 基本工资 ÷ 当月实际天数 × 缺勤天数。加班按相同小时工资计算，不使用倍数，且只有经管理层批准后才计入实发工资。保险和税款由人工录入，不自动进行法律计算。",
"خصم السلف والمكافآت والجزاءات المالية يظهر من السجلات المعتمدة للشهر. لا تعتبر السلفة مخصومة إلا بعد إغلاق المسير.": "预支扣款以及财务奖励和处罚取自该月已批准的记录。工资批次关闭前，预支不得视为已扣除。",
"يجب اختيار مجلد على قرص خارجي منفصل عن قرص تشغيل النظام. لا تحفظ النسخ على قرص C: إذا كان النظام مثبتًا عليه.": "请选择与系统运行磁盘分离的外部磁盘文件夹。如果系统安装在 C: 盘，请勿将备份保存在 C: 盘。",
"تحتوي النسخة على قاعدة بيانات PostgreSQL وصور الموظفين وملف تحقق ببصمات SHA-256. يتحقق النظام من سلامة ملف قاعدة البيانات قبل اعتبار النسخة ناجحة.": "备份包含 PostgreSQL 数据库、员工照片和 SHA-256 校验清单。系统会验证数据库文件完整性后才将备份标记为成功。",
"جدول النسخ والاحتفاظ / Schedule and retention": "备份计划与保留策略",
"نسخة يومية: الاحتفاظ بها 40 يومًا.": "每日备份：保留40天。",
"نسخة أسبوعية: الاحتفاظ بها 365 يومًا.": "每周备份：保留365天。",
"نسخة كاملة كل 3 أشهر: لا تُحذف تلقائيًا.": "每3个月进行一次完整备份：不会自动删除。",
"النسخ اليدوية ونسخ ما قبل الاستعادة لا تُحذف تلقائيًا.": "手动备份和恢复前备份不会自动删除。",
"المواعيد الافتراضية: يوميًا 02:00، الأحد 02:30، وأول يوم من كل ربع سنة 03:00. يمكن تعديلها من برنامج جدولة مهام Windows.": "默认计划：每天02:00、每周日02:30，以及每季度首日03:00。可在 Windows 任务计划程序中修改。",
"بعد تحديد مسار القرص الخارجي، يثبت مسؤول النظام مهام Windows مرة واحدة باستخدام ملف": "选择外部磁盘路径后，系统管理员只需使用以下文件安装一次 Windows 任务：",
"الاستعادة تستبدل بيانات النظام الحالية ببيانات تاريخ النسخة المختارة. ينشئ النظام نسخة كاملة من الحالة الحالية أولًا، ويتحقق منها، ثم ينفذ الاستعادة. بعد نجاحها ستحتاج إلى تسجيل الدخول مرة أخرى باستخدام بيانات المسؤول الموجودة في النسخة المستعادة.": "恢复操作会将当前系统数据替换为所选备份日期的数据。系统会先创建并验证当前状态的完整备份，然后执行恢复。恢复成功后，您需要使用恢复备份中的管理员凭据重新登录。",
"تاريخ سريان التعديل": "更改生效日期",
"سبب التعديل": "更改原因",
"لا توجد تغييرات مسجلة.": "没有变更记录。",
"أرشفة": "归档",
"إعادة تعيين": "重新聘用",
"لا يوجد موظفون مسجلون بعد. أضف أول موظف من النموذج أعلاه.": "尚未登记员工。请使用上方表单添加第一位员工。",
"مراجعة": "审核",
"طلبات تصحيح البيانات الشخصية": "个人资料更正申请",
"القيمة المطلوبة:": "申请更正内容：",
"سبب القرار": "决定原因",
"تاريخ السريان (للموافقة)": "生效日期（批准时填写）",
"تطبيق التعديل": "应用更改",
"رفض الطلب": "拒绝申请",
"لا توجد طلبات تصحيح بيانات.": "没有资料更正申请。",
"الهيكل": "组织架构",
"الفروع الحالية": "现有分支机构",
"إضافة القسم": "添加部门",
"حفظ القسم": "保存部门",
"إضافة فرع": "添加分支机构",
"حفظ الفرع": "保存分支机构",
"إدارة الإجازات والأرصدة": "休假与余额管理",
"يحسب النظام أيام الفترة التقويمية شاملًا يوم البداية والنهاية. أدخل أيام الخصم الفعلية يدويًا؛ لا يتم افتراض أيام الراحة الأسبوعية أو العطلات.": "系统按日历天数计算，包含开始和结束日期。请手动输入实际扣减天数；系统不会假设每周休息日或节假日。",
"الأرصدة الافتتاحية تدخل يدويًا حسب رصيد كل موظف. لا ينشئ النظام أي استحقاق تلقائي. التسوية الموجبة تزيد الرصيد والسالبة تخصمه.": "每位员工的期初余额均需手动录入。系统不会自动累计应得余额。正数调整增加余额，负数调整减少余额。",
"رصيد افتتاحي / Opening": "期初余额",
"تسوية / Adjustment": "调整",
"يمكن إضافة نوع مخصص. تفعيل تتبع الرصيد يعني أن اعتماد الإجازة يخصم الأيام من رصيد الموظف.": "可以添加自定义类型。启用余额跟踪后，批准休假时会从员工余额中扣除相应天数。",
"لا توجد حركات أرصدة بعد.": "尚无余额交易。",
"المساعد التنفيذي":"AI 工作台",
"يفهم أوامرك، يجهّز العملية، ويعرض التفاصيل قبل أي تنفيذ. التنفيذ يتم فقط بعد تأكيدك الصريح.":"理解您的指令，准备操作，并在执行前展示详细信息。只有在您明确确认后才会执行。",
"النموذج:":"模型：",
"— الاتصال بالنموذج مقصور على الجهاز المحلي.":"— 模型连接仅限本地设备。",
"السؤال العادي يُجاب عنه مباشرة. أي أمر تعديل يُحوَّل إلى خطة واضحة ثم تختار أنت الموافقة أو الرفض.":"普通问题会直接回答。任何修改请求都会先生成清晰计划，由您决定批准或拒绝。",
"البيانات الحالية قبل التنفيذ":"执行前的当前数据",
"التغييرات المطلوبة بعد التنفيذ":"拟议更改",
"الآثار والتنبيهات":"影响与警告",
"تفاصيل خطة التنفيذ":"执行计划详情",
"هذه الخطة محفوظة ببصمة. صلاحيتها محدودة، ولا يمكن إعادة استخدام الموافقة بعد التنفيذ.":"此计划已保存指纹且会过期。批准仅可使用一次。",
"أمثلة سريعة":"快速示例",
"اعتمد الإجازة رقم 12 — للمعاينة فقط.":"批准第 12 号休假申请 — 仅预览。",
"أضف سلفة تقسيط للموظف MY-0004 بمبلغ 3000 على 6 أشهر.":"为员工 MY-0004 添加 3,000 元分期预支，分 6 个月偿还。",
"مثال: نفذ: اعتمد مسير الرواتب رقم 3":"示例：执行：批准第 3 号工资单",

}

EN.update({
  "تاريخ الطلب": "Request Date",
  "المبلغ": "Amount",
  "تم حفظ مسار النسخ الاحتياطي.": "Backup location saved.",
  "الرواتب": "Payroll",
  "السبب": "Reason",
  "الشهر": "Month",
  "تنزيل ملف PDF / Download PDF": "Download PDF",
  "تعذر إكمال العملية:": "Operation could not be completed:",
  "التصنيف": "Category",
  "تسجيل «تم إطلاع الموظف» هو توثيق إداري بواسطة مسؤول الموارد البشرية بعد إطلاع الموظف أو توقيعه على المستند الورقي. هذا الإجراء ليس توقيعًا إلكترونيًا من الموظف.": "Recording “Employee Informed” is an administrative record made by the HR administrator after informing the employee or obtaining a signature on the paper document. It is not an electronic signature by the employee.",
  "ارفع ملف .xlsx يحتوي على التاريخ وأسماء موظفي الأمن المسجلين. اكتب وقت بداية الوردية أو OFF. المعاينة لا تحفظ أي ورديات.": "Upload an .xlsx file containing the date and names of registered security staff. Enter each shift start time or OFF. Preview does not save any shifts."
})

ZH.update({
  "تاريخ الطلب": "申请日期",
  "المبلغ": "金额",
  "ملاحظة:": "备注：",
  "حالة الشهر:": "月份状态：",
  "عرض أمثلة فجوات التغطية (حتى 20)": "显示考勤覆盖缺口示例（最多20项）",
  "الشهر في انتظار اكتمال الاستيراد وإنشاء المطابقة.": "本月正在等待完成导入并创建考勤核对。",
  "هذا الإدخال يسجل كحضور يدوي بواسطة مسؤول HR ولا يغيّر البصمة الأصلية. سيظل اليوم بحاجة إلى المراجعة والاعتماد قبل استخدامه في الرواتب.": "此记录由人力资源管理员手动录入，不会更改原始指纹数据。在用于薪资计算之前，该日期仍需经过审核和批准。",
  "بعد إنشاء مسودة المطابقة ستظهر هنا الأيام التي تحتاج مراجعة الموارد البشرية.": "创建月度核对草稿后，需要人力资源审核的日期将显示在这里。",
  "تم حفظ مسار النسخ الاحتياطي.": "备份位置已保存。",
  "الرواتب": "薪资",
  "السبب": "原因",
  "الشهر": "月份",
  "المسودة ← اعتماد الموارد البشرية ← إغلاق الشهر ← تسجيل الصرف النقدي أو التحويل البنكي. إعادة فتح مسير مغلق تتطلب سببًا مسجلًا في سجل المراجعة.": "草稿 → 人力资源批准 → 关闭月份 → 记录现金或银行转账付款。重新打开已关闭的工资批次必须在审核日志中记录原因。",
  "تنزيل ملف PDF / Download PDF": "下载 PDF 文件",
  "تعذر إكمال العملية:": "无法完成操作：",
  "ارفع ملف .xlsx يحتوي على التاريخ وأسماء موظفي الأمن المسجلين. اكتب وقت بداية الوردية أو OFF. المعاينة لا تحفظ أي ورديات.": "上传包含日期和已登记安保员工姓名的 .xlsx 文件。请填写每个班次的开始时间或 OFF。预览不会保存任何班次。",
  "القيم المالية يحددها مسؤول الموارد البشرية يدويًا. لا توجد معادلات تلقائية للمكافآت أو الجزاءات. يجب تحديد شهر الرواتب للإجراء المالي.": "财务金额由人力资源管理员手动输入。奖励和处罚金额不使用自动计算公式。财务操作必须指定薪资月份。",
  "التصنيف": "类别",
  "تسجيل «تم إطلاع الموظف» هو توثيق إداري بواسطة مسؤول الموارد البشرية بعد إطلاع الموظف أو توقيعه على المستند الورقي. هذا الإجراء ليس توقيعًا إلكترونيًا من الموظف.": "记录“已告知员工”是人力资源管理员在告知员工相关事项或员工签署纸质文件后所做的管理记录。此操作不是员工的电子签名。"
})

EN.update({
  "مساعد الموارد البشرية المحلي — M.Y. Business Hotel HR": "Local HR Assistant — M.Y. Business Hotel HR",
  "نظام الموارد البشرية — M.Y. BUSINESS HOTEL": "HR System — M.Y. BUSINESS HOTEL",
  "السلف | M.Y. Business Hotel": "Advances | M.Y. Business Hotel",
  "النسخ الاحتياطي والاستعادة | M.Y. Business Hotel": "Backup & Restore | M.Y. Business Hotel",
  "إدارة الإجازات | M.Y. Business Hotel": "Leave Management | M.Y. Business Hotel",
  "إدارة الرواتب | M.Y. Business Hotel": "Payroll Management | M.Y. Business Hotel",
  "مسير الرواتب | M.Y. Business Hotel": "Payroll Run | M.Y. Business Hotel",
  "التقارير | M.Y. Business Hotel": "Reports | M.Y. Business Hotel",
  "الجزاءات والمكافآت | M.Y. Business Hotel": "Sanctions & Rewards | M.Y. Business Hotel",
  "المساعد جاهز": "Assistant Ready",
  "المساعد غير متاح": "Assistant Unavailable",
  "المساعد المحلي جاهز.": "The local assistant is ready.",
  "خادم الذكاء المحلي يعمل، لكن النموذج المطلوب غير مثبت.": "The local AI server is running, but the required model is not installed.",
  "المساعد المحلي غير متاح حاليًا. النظام الأساسي يعمل بشكل طبيعي.": "The local assistant is currently unavailable. Core system functions remain operational.",
  "الاتصال بالنموذج مسموح فقط على الجهاز المحلي.": "Connections to the model are permitted only on this local device."
})

ZH.update({
  "مساعد الموارد البشرية المحلي — M.Y. Business Hotel HR": "本地人力资源助手 — M.Y. Business Hotel HR",
  "نظام الموارد البشرية — M.Y. BUSINESS HOTEL": "人力资源系统 — M.Y. BUSINESS HOTEL",
  "السلف | M.Y. Business Hotel": "预支工资 | M.Y. Business 酒店",
  "النسخ الاحتياطي والاستعادة | M.Y. Business Hotel": "备份与恢复 | M.Y. Business 酒店",
  "إدارة الإجازات | M.Y. Business Hotel": "休假管理 | M.Y. Business 酒店",
  "إدارة الرواتب | M.Y. Business Hotel": "薪资管理 | M.Y. Business 酒店",
  "مسير الرواتب | M.Y. Business Hotel": "工资批次 | M.Y. Business 酒店",
  "التقارير | M.Y. Business Hotel": "报表 | M.Y. Business 酒店",
  "الجزاءات والمكافآت | M.Y. Business Hotel": "奖惩管理 | M.Y. Business 酒店",
  "المساعد جاهز": "助手已就绪",
  "المساعد غير متاح": "助手不可用",
  "المساعد المحلي جاهز.": "本地助手已就绪。",
  "خادم الذكاء المحلي يعمل، لكن النموذج المطلوب غير مثبت.": "本地 AI 服务正在运行，但尚未安装所需模型。",
  "المساعد المحلي غير متاح حاليًا. النظام الأساسي يعمل بشكل طبيعي.": "本地助手目前不可用，系统的核心功能仍可正常运行。",
  "الاتصال بالنموذج مسموح فقط على الجهاز المحلي.": "仅允许连接本机上的模型。"
})


EN.update({
  "النموذج:": "Model:",
  "— الاتصال بالنموذج مقصور على الجهاز المحلي.": "— model connections are restricted to this local device.",
  "مثال: نفذ: اعتمد مسير الرواتب رقم 3": "Example: Execute: approve payroll run number 3"
})

ZH.update({
  "النموذج:": "模型：",
  "— الاتصال بالنموذج مقصور على الجهاز المحلي.": "— 模型连接仅限于本机。",
  "مثال: نفذ: اعتمد مسير الرواتب رقم 3": "示例：执行：批准第 3 号工资批次"
})


EN.update({
  "اعتماد شهور الحضور": "Attendance Month Approval",
  "المسؤول:": "Administrator:"
})

ZH.update({
  "اعتماد شهور الحضور": "考勤月份审批",
  "المسؤول:": "管理员："
})


EN.update({
  "مثال: study": "Example: study"
})

ZH.update({
  "مثال: study": "示例：study"
})


EN.update({
  "مثال: E:\\M.Y. Business Hotel Backups": "Example: E:\\M.Y. Business Hotel Backups",
  "مثال: استعادة بعد تلف قاعدة البيانات": "Example: restore after database corruption"
})

ZH.update({
  "مثال: E:\\M.Y. Business Hotel Backups": "示例：E:\\M.Y. Business Hotel Backups",
  "مثال: استعادة بعد تلف قاعدة البيانات": "示例：数据库损坏后恢复"
})


EN.update({
  "إرشادات الاستخدام": "Usage Guidance",
  "راجع الإجابة والبيانات المقترحة قبل الاعتماد عليها.": "Review the answer and suggested data before relying on them.",
  "أي إجراء يؤثر في بيانات النظام يحتاج إلى مراجعتك وتأكيدك قبل التنفيذ.": "Any action that changes system data requires your review and confirmation before it is carried out.",
  "إذا لم يكن المساعد متاحًا، يمكنك متابعة العمل من أقسام النظام الأساسية.": "If the assistant is unavailable, you can continue working in the system’s main sections."
})

ZH.update({
  "إرشادات الاستخدام": "使用指南",
  "راجع الإجابة والبيانات المقترحة قبل الاعتماد عليها.": "在采用回答和建议的数据之前，请先进行检查。",
  "أي إجراء يؤثر في بيانات النظام يحتاج إلى مراجعتك وتأكيدك قبل التنفيذ.": "任何会更改系统数据的操作，都必须先由您审核并确认后才能执行。",
  "إذا لم يكن المساعد متاحًا، يمكنك متابعة العمل من أقسام النظام الأساسية.": "如果助手暂时不可用，您仍可继续使用系统的主要功能模块。"
})

LANGUAGES = {"ar": "العربية", "en": "English", "zh": "中文"}

def _translate_value(value: str, language: str) -> str:
    table = EN if language == "en" else ZH if language == "zh" else {}
    if not table or not value:
        return value
    # Keep indentation and spacing exact; translate only authored text, never fuzzy user data.
    leading = value[:len(value) - len(value.lstrip())]
    trailing = value[len(value.rstrip()):]
    core = value.strip()
    if not core:
        return value
    translated = table.get(core)
    if translated is None:
        # Translate a known static label at the start, preserving dynamic suffixes and values.
        for source in sorted(table, key=len, reverse=True):
            if source.endswith(":") and core.startswith(source + " "):
                translated = table[source] + core[len(source):]
                break
    if translated is None:
        return value
    return leading + translated + trailing


class _Translator(HTMLParser):
    def __init__(self, language: str):
        super().__init__(convert_charrefs=False)
        self.language = language
        self.parts: list[str] = []
        self.skip_stack: list[str] = []

    def handle_decl(self, decl: str) -> None:
        self.parts.append(f"<!{decl}>")

    def handle_comment(self, data: str) -> None:
        self.parts.append(f"<!--{data}-->")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        raw = self.get_starttag_text()
        if tag in {"script", "style", "textarea", "pre", "code"}:
            self.skip_stack.append(tag)
        if tag == "html":
            lang = "ar" if self.language == "ar" else "en" if self.language == "en" else "zh-CN"
            direction = "rtl" if self.language == "ar" else "ltr"
            raw = re.sub(r'\blang\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', f'lang="{lang}"', raw, count=1, flags=re.I)
            raw = re.sub(r'\bdir\s*=\s*("[^"]*"|\'[^\']*\'|[^\s>]+)', f'dir="{direction}"', raw, count=1, flags=re.I)
        # Translate only authored presentation attributes. Never touch value/name/action/href.
        for name, value in attrs:
            if value is None or name.lower() not in {"placeholder", "title", "aria-label"}:
                continue
            translated = _translate_value(value, self.language)
            if translated == value:
                continue
            pattern = rf'(\b{re.escape(name)}\s*=\s*)(["\']){re.escape(value)}\2'
            raw = re.sub(pattern, lambda m: m.group(1) + m.group(2) + escape(translated, quote=False) + m.group(2), raw, count=1, flags=re.I)
        self.parts.append(raw)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        self.parts.append(f"</{tag}>")
        if tag in {"script", "style", "textarea", "pre", "code"} and self.skip_stack:
            if self.skip_stack[-1] == tag:
                self.skip_stack.pop()

    def handle_data(self, data: str) -> None:
        self.parts.append(data if self.skip_stack else escape(_translate_value(data, self.language), quote=False))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def get_html(self) -> str:
        return "".join(self.parts)


def translate_html(source: str, language: str) -> str:
    if language == "ar":
        # Still normalize document language and direction for the default locale.
        language = "ar"
    parser = _Translator(language)
    parser.feed(source)
    parser.close()
    return parser.get_html()
