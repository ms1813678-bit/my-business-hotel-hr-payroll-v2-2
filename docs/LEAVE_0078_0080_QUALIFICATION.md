# Bounded leave integration, 26 November2026

Base2967f535313a78a3b2599476c363bda83b8a9942. Development branch ui-overhaul only.

-0078: explicit HR completed-year W/H zero choice, exact one exclusion debit, immutable receipt, carry exclusivity. Negative balances remain debt. Separate manual cash-out is not prohibited.
-0079: explicit expired grant exclusion after verified complete monthly coverage and full day-use allocation. No automatic posting or FIFO.
-0080: current-epoch equal opening rebind with immutable lineage and no second credit. Exact retries require original actor/reason and current valid source/receipt.

## Actual Python3.11/PostgreSQL16 runs

| Run | Result | Boundary |
|---|---|---|
|financial2040|182 passed,110.04s|Financial/source/annual/carry/year transition/zero/math regression; no full year-two completion|
|zero2035|45 passed,89.90s|6 zero HTTP,6 expiry HTTP,3 rebind HTTP,11 restore gate,14 rebind,5 adversarial|
|rebind2038|26 passed,14.37s|14 rebind,5 adapted adversarial,4 independently authored replay-closeout,3 migration downgrade cases|
|expiry2041|22 passed,103.10s|21 expiry tests plus1 independent sequential4-phase zero HTTP acceptance|
|zero2039|6 passed,21.97s|Isolated zero HTTP, three languages, stale debit, epoch and second session|
|leave2045|24 passed,29.54s|Final replay and zero-HTTP regression after unused-local cleanup|
|leave2043|8 passed,48.71s|6 fresh-database two-session zero/expiry races,2 duplicate-epoch downgrade tests|

Runs overlap. Do not add their totals. Independent authorship does not mean independent execution on this exact candidate: final3.11 integration runs were performed by the integrator. No failed-test skipping. Earlier pre-fix failures were retained in local evidence and their focused corrected runs completed. No full-suite result is claimed.

Migration evidence:0078/79 populated FK-valid minimal rows make downgrade refuse transactionally without schema/data/version changes; these are not valid business receipts.0080 duplicate hashes across epochs refuse downgrade; compatible rollback and re-upgrade pass.

Static content/layout previews: Arabic390 review, English desktop review, Chinese390 saved inspected, with exact37/7,-37/7 and cash0/1. These are local static HTML renders, not a full JavaScript or browser transaction acceptance. Local preview asset resolution is separate from live branding. HTTP tests use actual auth, CSRF and migrated disposable databases.

## Remaining

Year-two net source/annual settlement remains guarded pending composition and full flow tests. Full encrypted backup/restore/account/balance acceptance remains open. Windows CI remains red and native parser security acceptance is not established. No new native or held attendance/offline experiment is adopted here. Hotel-device trial is last and outside progress percentages. No real employee data used.

Compiler/syntax and focused Ruff F/E9 checks pass. Broader style lint reports import-order and dict-style findings plus two pre-existing broad fail-closed catches; it is not claimed green.
