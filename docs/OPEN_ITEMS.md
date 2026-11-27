# Open items (tracked)

## Rounded approved amount and carried remainder (parked)

Status: not built, on purpose.

Context: the HR reconciliation approval stores only digests, the reason and who approved. It stores no amount and posts nothing to the ledger (the audit row records posted=False). The approval page shows the exact value. When the exact value is a rational with no finite decimal (for example 16/7), the page shows the exact fraction and a two-decimal number after the symbol "≈" for display only (ties to even, same rule as app.leave_exact_amount.display_days). The approved value is the exact one.

Proposal (parked, not a decision): once approved amounts are posted to the ledger, HR could see and approve a normal two-decimal number. The system would round once by the existing ties-to-even rule, store BOTH the exact value and the rounded approved value together with the rule, and carry the small difference forward so repeated approvals never drift or lose days. No blocking and no manual fixing. The team chose the display-only option for now and parked this until ledger posting exists; the final rounding design is still to be decided with Mo.

Why parked: this needs a stored rounded-amount and remainder field (a migration) and a ledger posting path. Neither exists yet. Build them together, with tests that repeat approvals and show zero cumulative drift.

## Refresh stage-source-manifest before desktop-native-prep work (tracked)

Status: not done, on purpose. No regeneration now.

Context: `verification/desktop_native_prep/stage-source-manifest.json` already had 77 of 285 entries whose hashes differ from the files at the base commit, and the desktop-native-prep stage fixture was failing its own hash check before the housekeeping bundle. The workflow only runs when files under `verification/desktop_native_prep/` change, so pushes elsewhere never exercise it.

Next step: regenerate the manifest as its own reviewed change, immediately before any desktop-native-prep work starts.

## Security roster department rule (fixed)

The roster coverage check used a different department rule from the attendance history function. For a date before an employee's first department change it returned the CURRENT department. Example: Sara worked in Reception until 10 Nov and moved to Housekeeping on 10 Nov. For 3 Nov the history rule says Reception (correct) but the roster check said Housekeeping. Both now use the history rule: start from the current department and undo every change that takes effect after the date.

## Roster department history: error wording (note)

`department_on` in the security roster check raises `employee_department_history_is_incomplete` for a malformed old value, while the attendance history function raises `employee_department_history_is_inconsistent` for its consistency check. Same malformed data, different message. Harmless; unify the wording if both paths are touched again.

## What setup seeds (decision by Mo, 27 Nov 17:35)

Setup pre-creates only structure rows: 8 departments (Reception, Security, Housekeeping, Maintenance, Administration, HR, Finance, Purchasing) and the approved shift patterns for Administration, Reception, Security, Housekeeping and Maintenance. It creates no employees, no balances and no sample records. The administrator account is created from the password typed at setup. `tests/test_fresh_install_has_no_business_rows_pg.py` guards the schema step.

## Four unused local variables left on purpose (triage one by one)

Ruff F841 still reports four assignments that were not removed because each could hide lost logic or a side effect. Check each before deleting:
- `app/attendance_reconciliation.py`: `has_fingerprint_evidence` is computed from unassigned fingerprint punches and never used. It may be a rule that was meant to be applied.
- `app/leave_opening_apply.py`: `effective = date.fromisoformat(payload['effective_start'])` is never used again, but it also rejects a malformed date. Keep the check, drop the name.
- `app/leave_month_review_routes.py`: `target` (parsed month value) is not used after assignment.
- `app/leave_synthetic_register.py`: `employee = db.get(..., populate_existing=True)` is not used; the call refreshes the row.
The CI gate (`tests/test_pyflakes_gate.py`) checks F401, F811 and F601 only, so these four do not fail CI.
