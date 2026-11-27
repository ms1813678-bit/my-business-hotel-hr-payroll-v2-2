# Project specification — M.Y. Business Hotel HR System

## Product goal
A local-only Arabic / English / Simplified Chinese HR and payroll application for M.Y. Business Hotel, Maadi, Cairo. The system is operated by one authorized administrator: the HR Manager. It is not a public website and must not expose its local service to the public internet.

## Runtime and architecture
- Target: Windows 10 or Windows 11 laptop, 16 GB RAM recommended, approximately 500 GB storage.
- Local web application with PostgreSQL as the database and local file storage.
- Manual launch from a desktop shortcut; do not auto-start with Windows.
- Arabic is the default language; support English and Simplified Chinese, RTL for Arabic and LTR for English/Chinese, Africa/Cairo timezone, DD/MM/YYYY, 24-hour time, and EGP.
- Core HR/payroll functions must work offline. The intelligent assistant is a core UI component; local model availability must not block normal HR/payroll operation.
- Prefer a free local AI model. Do not use paid APIs, subscriptions, or external data services without explicit approval.
- No cloud hosting, cloud database, or external processing of hotel or employee data is in the current scope. Core HR and payroll functions must operate locally.

## Access and security
- Three named logins only: the HR manager (system administrator), the assistant manager and the finance manager, each with its own role and permissions (decision notebook section B; corrected 27 Nov 2026, the earlier text said exactly one login). No employee, supervisor, or department-manager accounts.
- Username and password with secure password hashing; never reversible password storage.
- Keep the app local; do not expose it publicly.
- Audit consequential changes only, recording actor, timestamp, action, previous value, new value, and reason.
- Protect the laptop and external backup drive.

## Modules
1. Dashboard
2. Employees
3. Attendance
4. Leave
5. Payroll
6. Advances
7. Sanctions and rewards
8. Reports
9. Optional AI assistant
10. Settings and backup

Future modules, not part of the initial release: purchasing, inventory, accounting, maintenance, assets/custody, and expenses. Payroll must not be linked to accounting or expenses.

## Employee records
- Organize Hotel → Department → Branch → Employees.
- Editable departments: Reception, Security, Housekeeping, Maintenance, Administration, HR, Finance, Purchasing.
- Branches are configurable within departments.
- One active department and branch per employee; preserve transfer history.
- Generate employee codes MY-0001 onward; never reuse a code.
- Store full name, national ID, phone, address, optional photo, fingerprint ID, department, branch, position, hire date, status, basic salary, daily work hours, probation dates, leave balances, attendance, sanctions/rewards, and change history.
- Do not implement contract-expiry management or document uploads in the initial release.
- Allow HR to make retroactive changes with effective date, record date, and reason.
- Terminated employees are archived. Rehire uses the same code but starts a new service period while preserving previous history.
- Employee requests for personal-data changes are recorded; HR applies changes with old/new history.
- No employee master Excel file currently exists; initial records are entered manually. Provide an import template later.

## Security roster rules
- Security coverage is validated by day before attendance can be finalized.
- Never allow fewer than 2 guards assigned to work on a day, never allow a 1-guard day, and never allow an all-off day.
- Determine the shift model from the number of active Security employees registered in the system, not from the number present on a particular day: exactly 2 active registered guards use two 12-hour shifts; 3 or more active registered guards use three 8-hour shifts. OFF never changes the model. At least 2 guards must be assigned to work every day; all required coverage windows must be covered without gaps or overlap. Additional guards may share a configured 8-hour shift.
- Shift start/end times remain configurable; the system must not invent times.
- A day must have continuous coverage with no gaps and no overlapping assignments.
- Overnight shifts and midnight boundaries must be handled explicitly; ambiguous patterns are sent to HR review rather than guessed.

## Attendance imports and review
- Monthly inputs: the attendance program's one-sheet report is the primary source; the ZKTeco LX50 fingerprint export supplements missing days/events and is checked across the full month. Preserve the user's source priority: program report first, fingerprint report second. The program report defines the monthly period; when the fingerprint export contains only dates with punches, align its contained date range to that same program month rather than creating a shorter separate month.
- Preserve the exact original uploaded workbooks, SHA-256 hashes, raw event values, employee identifiers, sheet names, and row positions; authorized HR users can retrieve the original source file later.
- Fingerprint exports may contain multiple time tokens in one cell; preserve every token and its original cell value. Support the ZKTeco `Att.log report` matrix layout: day numbers are columns, each employee has an `ID:`/`Name:` metadata row followed by that employee's daily punch row, and adjacent time tokens without separators must remain distinct events. Ignore schedule/statistics/exception summary tabs as raw punch sources; retain the original sheet and row for every event.
- Non-Security departments infer the shift from the first fingerprint within the department's configured shift windows effective on that work date; historical department transfers must not apply the employee's current department retroactively. Security alone uses a separate monthly roster workbook containing every currently active Security employee; the roster is not limited to three employees.
- A blank cell or missing row is not proof of presence, absence, or a weekly rest day.
- Match employee identities only by exact registered employee code/fingerprint ID/name aliases. Unknown identities remain unresolved until HR explicitly maps them to an employee; preserve the original identity and audit who resolved it.
- Flag entry-without-exit, exit-without-entry, missing fingerprints, duplicate events, unknown IDs, and conflicts with leave/absence. Never invent missing fingerprint times.
- An import batch with unresolved parsing/identity issues cannot enter monthly reconciliation. HR may resolve an unknown identity explicitly; otherwise replace a rejected/unresolved source batch with a corrected workbook before reconciliation. Keep the superseded batch and its source evidence for audit.
- Monthly status: Imported → Under Review → Approved. Payroll cannot be finalized until attendance is approved.
- A monthly attendance record is created when the first source file for an exact period is imported; the program and fingerprint batches must both be linked to the same exact period before reconciliation.
- Reconciliation moves the month to Under Review. Monthly approval is a separate HR action and is blocked while any generated daily record is not approved, either source batch has unresolved import issues, or either source is missing.
- Monthly approval requires an explicit administrator attestation that all employee/day coverage gaps were checked against both source reports. The review page lists the total gaps and up to 20 examples; the approval audit records the gap count. Historical employee service periods must be considered even after a later rehire updates the employee's current hire date. For Security, an approved roster's explicit OFF cell exempts only that employee-day; every employee assigned to work still needs an attendance record. Because blank source cells are not proof of absence, missing rows must never be silently converted to absences.
- Unresolved absence is never classified as final absence.
- No mobile/GPS attendance, no employee self-check-in, and no individual advance shift roster.
- Support overnight shifts and midnight boundaries. Ambiguous shift patterns must be flagged, not guessed.
- Shift matching uses separately configurable before/after margins, each capped at 120 minutes; effective windows are reduced as needed so neighboring shift windows never overlap. Exact midpoint minutes between neighboring starts are left unmatched rather than guessed. Duplicate shift start times (including Security's two 08:00 starts) remain ambiguous to clock-based matching and require the applicable roster/administrator input.
- AI-assisted attendance review is advisory: it analyzes imported records, proposes matches with reasons, and presents exceptions for HR review. Clear cases may be prepared automatically under approved rules, but attendance is not finally approved and consequential records are not saved as approved without the administrator's confirmation. Core attendance workflows remain usable if AI is unavailable.
- Security monthly roster rule: the uploaded Excel roster must contain one column for every currently active Security employee, each showing that employee's configured shift start time or explicit OFF for each date. The number of active guards is not fixed at three. The shift model is selected by the full active Security employee count, including those marked OFF on that date: exactly two active guards use the two configured 12-hour shifts; three or more active guards use the three configured 8-hour shifts. OFF never changes the model. At least two employees must be assigned to work each day, and all required coverage windows must be covered; an incomplete roster is sent for HR review rather than silently falling back to another model. 00:00 is a valid shift start, never an OFF marker. End times must come from the matching configured Security shift pattern, including overnight boundaries; the system must not invent a missing end time. Fewer than two working employees, incomplete 24-hour coverage, missing patterns, or ambiguous configured pattern definitions require administrator review. Employees sharing the same configured shift start is allowed and is not itself a duplicate-pattern error. AI may parse and propose the roster, but the administrator confirms before assignments are saved as approved.
- Configurable shift patterns:
  - Administration: 09:00–17:00
  - Reception: 07:00–15:00, 15:00–23:00, 23:00–07:00
  - Security: 08:00–20:00, 20:00–08:00, 08:00–16:00, 16:00–00:00, 00:00–08:00
  - Housekeeping: 08:00–16:00, 16:00–00:00, 09:00–17:00
  - Maintenance: 09:00–18:00
- Daily hours are defined by job/employee; do not assume every role is eight hours.
- Monthly standard hours = daily hours × 30.
- Grace period: 10 minutes late and 5 minutes early departure. Calculate actual net minutes/hours after grace; do not round 30 minutes to a full hour.
- Track late and early-departure counts and total minutes separately.

## Leave
- Types: annual, sick, casual/emergency, official holiday, weekly rest, unpaid.
- Initial balances are entered manually. Never invent entitlements or balances.
- Administrator manages leave directly; no employee self-service or manager approvals.
- Leave is recorded and reviewed by the authorized operators, not submitted as employee or department-manager requests. Recording may be retrospective and has no request-only72-hour notice gate.
- Deduct a balance only once leave is confirmed/recorded. Preserve balance history.
- Entitlements remain configurable.

## Payroll
- Fixed monthly base salary, not linked to hours worked. Hours are used only for agreed deductions/additions.
- Pay date: day 5 of the following month.
- Payroll fields: base salary, attendance bonus, performance bonus, occupancy bonus, overtime, other bonuses, absence deduction, lateness deduction, insurance share, taxes, other deductions, advance deduction, net pay.
- Insurance/tax fields are reserved; no automatic legal/tax calculation in the initial release.
- Bonuses and other payroll items are manual; no invented bonus formulas.
- Monthly standard hours = daily hours × 30.
- Hourly rate = base salary ÷ (daily hours × 30).
- Late/early deduction = hourly rate × actual net hours after grace.
- Absence deduction = monthly salary ÷ actual calendar days in month × absence days. Weekly rest is not deducted.
- New hire mid-month: salary ÷ actual calendar days × payable days from hire date through month end, including hire date.
- Overtime requires management-requested additional work, not voluntary staying. HR reviews at month end and decides eligible hours. Same hourly rate; no multiplier.
- One administrator previews and approves payroll. Attendance must be approved first.
- Month closes after approval. Reopening requires a reason and audit record of old/new state.
- Payment methods: cash or bank transfer. Track unpaid/partially paid/fully paid, amount, actual date, and method.
- No transport or meal allowances.

## Advances
- Requests are accepted from the 15th through the 22nd of the current month; future-dated and prior-month requests are rejected.
- Regular monthly advances alone are subject to the 25% of basic salary cap for the relevant accounting month. Pending, approved, and paid regular advances for that same accounting month reserve the cap until deductions are applied.
- Installment/large advances are a separate type and are not subject to the regular monthly 25% cap. The administrator approves a repayment schedule of 1–36 installments; installment amounts are distributed to cents and sum exactly to the original advance.
- The first installment belongs to the request month. Each installment is deducted in its accounting payroll month and paid on the 5th of the following month. Example: a request on 19 October for 6,000 over 6 installments is accounted for October–March and paid on 5 November–5 April.
- Payroll consumes only the installment due for its payroll month, not the full original installment advance. Reopening payroll reverses the installment application without losing its schedule.
- Show latest advance, date, type, original amount, deducted, outstanding, and regular-advance available limit separately.

## Sanctions and rewards
- Sanctions: verbal notice, first warning, final warning, salary deduction, suspension.
- Administrator records and approves. No employee acknowledgment workflow.
- Deduction amount is manual and flows into payroll.
- Rewards: financial reward, certificate, commendation, promotion recommendation, and configurable types. Financial amount is manual. Preserve history.

## Reports and dashboard
- Reports are generated **manually by the administrator from the Reports module**; there is no automatic monthly report job in the initial release.
- The administrator selects the report type and an explicit start date and end date.
- The administrator can select **all employees** or **one specific employee**.
- Employee selection must be applied consistently to every report type where an employee filter is meaningful; department/branch filters may be offered where supported.
- Available report periods include daily, weekly, monthly, and arbitrary custom ranges through the same manual date-range controls.
- Excel and PDF exports.
- Reports: employee, attendance, leave, sanctions/rewards, payroll, advances, and department comparisons.
- Generated reports are saved locally for download/archival.
- Intended email recipient is not part of the automatic report workflow; if a future email action is added, it must require an explicit administrator action and configured credentials. Never claim delivery unless confirmed.
- Dashboard shows counts, attendance, incomplete records, leaves, payroll, alerts, department rankings, and previous-month comparisons.
- Never present unresolved absence as final.

## Optional AI assistant
- Administrator only. Free local model preferred; evaluate compatibility/performance on a 16 GB Windows 10 or Windows 11 laptop.
- Model download may require internet once; offline operation afterward.
- System must remain fully usable when AI is disabled or unavailable.
- Natural-language assistance for employee search/analysis, Excel parsing/mapping, leave, payroll preview, advances, sanctions/rewards, and reports.
- Preview and confirm before saving. Never invent missing data, approve payroll/leave automatically, or change consequential data without administrator confirmation.
- Never guess unknown Excel columns; ask only when genuinely necessary.
- No voice AI and no API bypass.

## Backup and recovery
- External-drive backups: daily, weekly, quarterly, manual, and pre-restore archives are never deleted automatically; the administrator deletes old archives manually when storage requires it.
- Full backup includes database, current/former employees, raw fingerprint imports and attendance, leave, payroll, advances, sanctions/rewards, departments/branches/positions, users/roles/settings/audit, and relationships.
- Verify integrity and perform restore tests. On failure, alert and retry; report success only after verification.
- Restore requires sole administrator confirmation, displays backup date, records reason, and preserves current state first when possible.

## Delivery principles
- Implement in small, reviewable stages.
- Do not claim a feature is complete until code and tests support that claim.
- Preserve source imports and historical records; use database migrations for schema changes.
- Add automated tests for payroll calculations, date boundaries, duplicate imports, permissions, and backup/restore before production use.
- No production deployment or real employee data until local installation, security, backup, and restore have been tested on the target Windows 10 or Windows 11 laptop.


## Database migration rollback policy

- Production databases must not be downgraded across migration `0009_audit_log_immutability`. Its downgrade removes database-level UPDATE/DELETE triggers from `audit_logs`, weakening tamper protection.
- For production schema corrections, prefer a forward-only migration that preserves audit immutability.
- If a rollback is unavoidable for disaster recovery, stop application writes, take and verify a full backup, restore into an isolated environment, and explicitly re-establish and verify the audit-log triggers before reopening the system.
- Automated migration downgrade tests may exercise rollback in disposable test schemas only; they do not authorize production downgrade.
