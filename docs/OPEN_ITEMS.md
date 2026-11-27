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
