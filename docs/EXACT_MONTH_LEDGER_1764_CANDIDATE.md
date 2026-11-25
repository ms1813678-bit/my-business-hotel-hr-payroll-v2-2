# Exact monthly leave ledger and review

Integrated bounded slice based on feb849c54f7351a9ba15fcfd8f6593eaa68c6cd6, migrations 0070 and 0071. Publication does not establish deployment or hosted acceptance.

## Exact amounts and one ledger

Legacy finite days_delta values remain unchanged. Repeating values use NULL days_delta with reduced integer exact_numerator/exact_denominator. Database constraints prohibit dual, missing, nonfinite, zero or unreduced rational representations. There is no rounded shadow value. Terminating results use the legacy finite representation. The shared row-based adapter supplies workflow, manual/AI review, main-page balances, reconciliation, source snapshots, reports and ledger export. Presentation rounds to two places only; exact exports use finite text or n/d. Old finite-only source keys remain absent, preserving their canonical representation.

## Monthly operation

Only HR reviews and confirms an exact saved department source. Signed review binds the source, employee, date, mode, reason, actor, session/version and restore epoch. Real CSRF and existing restore gates remain enforced. POST takes the bounded exclusive transaction gate before reading the saved source; GET uses the existing shared read gate. Commit retains the gate through render. Missing or changed evidence refuses rather than inventing attendance or policy.

Earning uses actual calendar days, the explicit tier and U/S policy, complete service and factual register, and the verified opening effective cutoff. Months post sequentially. Employee/month uniqueness and immutable receipt prevent duplicate posting with a different operation key. Existing register A/W/H facts are debited explicitly once, not presumed already debited. Held annual earning is recorded but not credited until the first full calendar work month is proved. Standalone release can credit prior held earning later without posting another month or repeating usage. Saved receipts verify their linked results on retry. Changed historical earning evidence blocks new release.

Usage during the holding period can make the ledger negative. The UI explains that posting is not leave approval or an automatic overdraft judgment. Posting does not change payroll, attendance, month closure or approvals. Approved/closed overlapping payroll and closed leave periods refuse.

## Limits

This is a bounded monthly slice. Cross-year carry, settlement, correction workflow and broader leave completion are not supplied by these migrations. Unsupported cross-year inputs fail closed. The existing held synthetic journal, transition, native and device experiments remain held. Windows isolated fingerprint parsing remains unavailable, so normal source creation is qualified on supported POSIX only. No hotel-device trial is claimed; that remains last and outside progress percentages.

## Qualification, 25 November 2026

- Python 3.11.18, PostgreSQL 16.2: combined selection 196 passed, 3 existing SQLite-only skips, 56 warnings in 78.76 seconds. This includes migrated rational constraints and guarded down/up, mixed readers/manual adjustment, AI gateway, reports, source/reconciliation, UID dump/restore, migration regressions and real HTTP in AR/EN/ZH.
- Three HTTP wrappers require exactly one child JUnit test, zero failures/errors/skips, with portable temporary captures. Actual authentication/CSRF, signed review, held month, release-only replay and exact balances pass.
- Deterministic overlap pauses the first request after the real exclusive guard. A second authenticated request receives truthful 503 with Retry-After while the first remains active and no receipt exists. Releasing the first gives 200; exact retry gives 200 with one receipt. No security guard is replaced or bypassed.
- Separate internally prepared payroll/rollback/replay guard tests: 9 passed, 1 warning in 2.67 seconds. Test defects were repaired using actual fixture imports and a SQLAlchemy before_insert failure hook; no external-model authorship is claimed.
- Actual AR 390-pixel, EN desktop and ZH saved-state images inspected by writer and main. Wrapped headers, approximate/exact distinction and translated human transaction labels verified for those shown states only.
- Independent focused financial review accepted the 1790 source and 1792 forced-overlap test, static only. Reviewer did not execute the pass counts. Independent consumer qualification later reproduced and repaired a Fraction overlap-digest encoding defect. Its repaired 16-test consumer set and existing overlap plus release guards passed locally, 39 tests total.
- Local UID dump initially failed because pg_dump 14.24 targeted server 16.2. Exact stderr confirmed the mismatch; selecting installed matching PG16 tools passed. No product preflight change is included here.

Final broad local leave/workbook selection: 648 passed, 2 existing skips, 80 warnings in 89.22 seconds on synthetic_leave_1765. The held register-cycle harness requires its separate synthetic_cycle prefix: 13 passed, 1 optional manual-cycle skip. Running both harnesses under one name triggers their intentional incompatible name guards; neither guard was weakened. New annual release negatives: 4 passed, covering early/missing receipt, replay identity/date/reason/digest, cross-year refusal and insert-failure rollback/retry. These counts overlap earlier selections and are not summed.

Full hosted Linux/Windows qualification and hotel-device acceptance are not claimed.
