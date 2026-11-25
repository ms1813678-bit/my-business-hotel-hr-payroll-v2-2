# Monthly report date preflight, Instinct, 25 November 2026

## Observed defect and correction

The retained extreme-date lead in MONTHLY_REPORT_FINDINGS_1697.md was investigated against published b6134e4. Three actual route-body probes (January 0001 payroll summary, May 0001 six-month chart, December 9999 leave) already returned HTTP 400 because the builder raised ValueError. They did **not** reproduce a monthly HTTP 500. Their error details were Python implementation messages about year 0 or 10000, without the existing Arabic/English/Chinese invalid-month translation, and validation happened after entering the route database context.

`monthly_date_bounds` now checks the selected paths' required arithmetic in both the route and the builder. The route checks after option/role authorization and before opening its report database session, rendering or saving a copy. The builder independently applies the same check for direct callers. A rejected date uses the existing translated invalid-month message.

- Every monthly report currently constructs the next month's first date, so December 9999 cannot be represented.
- Payroll/attendance options 1–4 compute the preceding month even when the comparison modifier is not selected, so January 0001 cannot serve those existing paths.
- Six-month charts 8–9 require five preceding months, so they require June 0001 or later.
- Options 5–7 do not acquire a new early-year restriction. January 0001 remains valid for those paths.

These are representability checks, not an invented business-year policy. They do not silently remove sections or shorten a chart. The custom-report date resolver and arbitrary custom-period arithmetic are unchanged and not claimed fixed. No role, schema, selection, date basis, business write, calculation, restore, native or device behavior is changed. No blanket catch masks errors in other code.

## Qualification

Verified: Python 3.11.18, PostgreSQL 16.2 configured at the real migrated 0067 head; existing report/role/security fixtures retain their own documented SQLite storage where used. 180 passed, zero skipped, two warnings, final shared run, see below, including 44 new boundary tests and existing monthly/custom report, role and route-security suites. Do not describe all 180 as actual PostgreSQL behavioral tests.

New tests include direct builder rejection before a database access, direct route rejection before renderer/save, nine real TestClient HTTP localized refusals in AR/EN/ZH, and five actual PDF HTTP neighbor cases with correct saved-copy period bounds (January 0001 leave, February 0001 payroll, June 0001 chart, November 9999 chart, February 2024 leap month). Removing the route preflight causes all 12 relevant early-refusal tests to fail. Original-source probes preserve the existing-400 result.

No layout change is intended or claimed. PDF magic/status and save bounds are checked here, not a new visual or hotel-device acceptance. Independent static source review accepted the minimal diff and option arithmetic mapping against b613; tests were not independently rerun. Modifier-only 10/11 requests retain their previous no-data rejection order, explicitly tested with nine cases. Final shared-tree result is recorded below.

Final shared-tree qualification: 180 passed, zero skipped, two warnings, 32.42 seconds; 44 new boundary tests including nine preserved modifier-only rejection cases. Independent acceptance was static source review, not test execution. No visual/layout acceptance claim.
