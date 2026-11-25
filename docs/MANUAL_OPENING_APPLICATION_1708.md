# Manual A/W/H opening application, checkpoint 1708

25 November 2026. This is a bounded manual opening delivery, not completion of the financial leave workflow or the project.

## Delivered behavior

The existing manual-opening review page now offers an inline, explicit application step after source confirmation. It preserves the three exact A/W/H balances, basis date and source, requires a separate reason and human checkbox, and displays a stored receipt after success. Source confirmation alone never posts balances. The owner selected this same-page design on25November20:03.

The server validates the current actor, session, epoch, source, canonical category mapping and exact digest. Ordered table intents serialize the plan with existing writers. Each nonzero amount creates one immutable opening ledger row; zero remains covered by the aggregate immutable audit receipt, not a fake zero ledger transaction. Retry returns the same receipt. A new selection cannot duplicate an already-applied employee baseline, including an all-zero baseline. Legacy single-category opening writes share the guard; later adjustments retain their existing rules.

Migration0068 adds a strict manual-opening source identity distinct from monthly earned/released credit metadata, enables balance tracking only for canonical weekly-rest/official-holiday types, and preserves existing identities/history/values. No ledger backfill. Downgrade refuses before DDL when manual-opening identity would be lost; it does not guess historical tracking flags. The model has an equivalent portable strict CHECK for legacy SQLite unit fixtures; migrated PostgreSQL retains anchored hex checks.

Read-only reconciliation accepts only fully verified receipts matching the active confirmed source and exact immutable ledger/audit evidence. Unverified manual-prefixed rows, including other selections, remain unresolved with no invented target or delta. Existing register day facts are preserved and projected once; they are not misclassified as ledger postings. Existing ledger, noncancelled leave and relevant closed/payroll evidence still refuse incompatible opening application. No automatic accrual, release, settlement, cash or year-close is enabled.

## Qualification

Supported Python3.11.18 and PostgreSQL16.2. Complete default inventory:193 test files,2605 nodes executed in13 disjoint runs,2558 passed,47 existing skips,zero failures/errors. This is a chunked complete inventory, not one process or all-PostgreSQL proof. Existing fixtures include SQLite. All skipped IDs/reasons and JUnit identities are retained in the qualification manifest. No acceptance flag or database-name guard was weakened.

The default result includes the disposable-database HTTP wrapper. Its four child tests exercise the real application login, signed session, CSRF and current database permissions in AR/EN/ZH, the full source-to-inline-application-to-receipt path, retry and malformed/stale/superseded requests. A PostgreSQL-observed apply lock wait leaves unrelated static HTTP responsive. Child cases are not counted twice. Dedicated kernel races and receipt corruption cases are included. The wrapper uses pytest temporary paths and refuses any child failure/error/skip; it does not skip PostgreSQL privilege failures.

The full inventory caught and repaired a real candidate SQLite metadata regression and an unmapped AI approval refusal. The latter has explicit AR/EN/ZH coverage; the manual-opening HTTP route still uses its existing generic localized error. Legacy-surface reachability is not established by the display tests.

Two stale concurrency tests expected blocking where published1709 deliberately returns neutral503. The default overtime test now checks exact503 while the holder still owns its advisory lock, successful table access without a cycle and later helper success. A separately enabled existing synthetic-transition test initially failed for that stale expectation. Its test-only handling now permits exact busy503, requires a single winning operation, validates exact replay after release and refuses a second key with operation count one. That enabled case passed on a fresh dedicated migrated database; it remains separate from the default47 skips and is not added to the default pass count.

Final exact shared-tree selection:255 passed,zero skipped,35 warnings in35.49 seconds. All22 product/test files were byte-compared with the qualified isolated source.

Actual desktop AR/EN/ZH review and receipt screenshots were inspected. Arabic354px review was inspected: stacked balances preserve the displayed long exact decimal. Post-receipt mobile layout is not established by that screenshot. This is not Windows/native/hotel-device acceptance.

## Authority and remaining boundaries

Owner originals recovered from authenticated WhatsApp history:25November01:40:48 authorizes continued implementation/publication without repeated upload approval;20:03:47 selects inline choice1. Business source originals retain manual three-balance opening, explicit negative-opening refusal and separate historical registration. Technical review is not a new business grant.

main is untouched. Held0066 attendance, journal/HTTP/admission/native experiments and AI model/config candidates are not adopted. Current Windows startup failure remains unproved/unfixed. Actual hotel-device trial is last, after development, outside progress percentages. No whole-project or financial-flow completion follows from this checkpoint.
