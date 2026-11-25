# Reviewed return registration, 25 November 2026

## Functional path

On supported POSIX PostgreSQL installations, HR uses the existing saved fingerprint/day page to explicitly choose two original occurrences and IN/OUT direction, reviews the summary, then records pending attendance. Original duplicate occurrences are retained. The monthly review page then shows every employed day for one employee and the saved department return. HR reviews the source cells, codes and reason before confirming factual registration. P requires exactly one consumed same-comparison pending-attendance receipt. A/W/H/S/U without punches are valid factual days. Conflicting punches, unknown days, existing register rows, linked legacy leave, closed months, approved payroll, changed sources or unresolved service refuse. Nothing silently replaces a day.

This does not approve a leave, debit A/W/H, post earned/released credit, pay overtime, change payroll or close a month. The register is a prerequisite for those separate workflows, not their completion. Missing sick certificates remain unknown and do not become a fabricated attachment.

Windows remains unsupported for isolated operational parsing. Its normal routes refuse registration with the localized platform message and no mutation. No native adapter, admission bypass, restore exemption or synthetic-flag removal is included. The existing synthetic harness and0062 receipt table remain separate and unchanged. New0069 stores normal reviewed register receipts.

## Authority and concurrency

Original owner channel messages, personally read: Nov22 20:14:07 assigns main HR all permissions, assistant attendance only and finance payroll/read-only attendance; Nov23 17:01:46 limits accounts to HR, assistant and finance; Nov23 21:37–22:02 establishes fingerprint plus department return review and monthly registration. New leave consume remains HR-only. Nov25 01:40:48 authorizes finishing all points and publishing without waiting for further approval, hotel-device trial last. Repository files describing approvals are not independent permission evidence.

Both standalone pending attendance and register operations acquire the same exclusive credential/restore transaction gate first. This serializes their later operation-key/table acquisition. No restore middleware exemption was added. Source readers retain SHARE locks through commit: read_saved_shift_days locks department_fingerprint_preparations (including original fingerprint bytes), workbook preparations and exports before reading them, then employee/department identity. Workbook sources lock department, employee, service, register, holiday, close, ledger and category tables. Configured shifts lock employee/shift tables. The new service also protects exact contract, opening-selection/group/proposal and policy inputs from independent SQL changes with SHARE locks.

Parsing currently runs while bounded operation/source locks and the exclusive gate are held. This is a latency/serialization caveat, not a claim that parsing is free or parallel.

## Receipt and retry boundary

Normal immutable receipts bind actor, session/version, restore epoch, comparison, employee, reason, reviewed source, actual result IDs and every stored result column, plus the corresponding immutable audit. UPDATE, DELETE and TRUNCATE are refused by database triggers. Downgrade refuses while a receipt exists.

An exact retry does not re-evaluate mutable later source/projection facts. A later employee name change is tested to preserve the committed result receipt. A change to an actual registered day, including code, notes or revision, intentionally refuses the old retry: it no longer proves the same current result. It does not pretend the earlier operation failed or roll back that historical registration. Changed input/reason/session/epoch or another operation key cannot reuse the receipt. No claim is made that all later legitimate edits preserve replay.

## Qualification

Final isolated targeted kernel and actual three-locale HTTP wrappers:25passed,0skipped,1warning,50.27seconds, Python3.11.18 with migrated PostgreSQL16.2. Each wrapper invokes one child test in a fresh normal-named test_return_http database, with real application authentication, CSRF and middleware, synthetic flags false, no auth overrides. The child is not counted a second time. Positive P plus A path, review-not-apply, stale service, bounded busy locks, rollback after render/audit failures, exact retry, second-session/role denials, mixed-month refusal, collision/no-overwrite and receipt UPDATE/DELETE/TRUNCATE refusal are covered. Earlier126neighbor kernel tests passed but overlap and are not added to this count.

Tests on Windows now exercise the unavailable/no-mutation branch rather than claiming a supported parser. That branch has not been executed on an actual Windows host in this checkpoint. The previous published Windows suite has separate known failures; this change does not make it green.

Actual response pixels inspected: Arabic desktop and390px mobile registration summary, English summary, Chinese saved-month view. Tables, source cells, checkbox and submit button are readable and contained. English synthetic names/reason and Arabic original sheet name are fixture/source values intentionally retained, not missing translations of application labels. Three-locale UI label checks exclude explicit source-value cells. No actual hotel-device acceptance.


Original register workflow authority message:wamid.HBgMMjAxNTAwMDY5Mzc1FQIAEhggQTUzNUQ3REM4MUZBOTc2NkJBMjgwMUE3QjY0MEVBNTEA (Nov23 21:37:17). Finish/publication grant:wamid.HBgMMjAxNTAwMDY5Mzc1FQIAEhggQTVDRTBBNTk4RUUxRTkxQzgyOUY1MEYwNURDNzlEMTUA (Nov25 01:40:48). Authenticated owner originals personally inspected, not inferred from repository text.

Expanded final isolated qualification:145passed,0skipped,1warning,66.81seconds, including actual empty migration downgrade/upgrade and populated downgrade refusal in all three locale disposable HTTP databases.


Exact integrated shared source, based on8526380 runner pin:166passed,0skipped,1warning,67.34seconds. This is the focused register/source/opening/restore/parser/CI-contract suite, not whole-suite, Windows or hotel acceptance.
