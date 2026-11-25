> Historical record below. Current branch, candidate identities, test outcomes and open work are in [CURRENT_SOURCE_STATUS.md](CURRENT_SOURCE_STATUS.md), updated25November2026. Old HEAD/readiness/pending-publication statements below describe their original checkpoint, not current state. No history has been removed.

# M.Y. Business Hotel HR & Payroll: current development acceptance

Prepared 24 November 2026. Review copy, not an accepted release or installation instruction for real staff data.

## Version and publication

Repository: ms1813678-bit/my-business-hotel-hr-payroll-v2-2. Development branch: ui-overhaul. Three reviewed commits were published to ui-overhaul on24November2026: foundation d57ca2afb9fed0562084a7fe589b6fde9a085114, application d7644e0493bc664c9f3bd797d8488a37050089f6 and HR approval 2eac8fba5fce966214ca1d04e1b9780703c4ee48. The remote branch was independently verified at approval commit2eac8fba5fce966214ca1d04e1b9780703c4ee48. Its original-workflow run45448595426 has a failed Linux suite job; Windows and the overall outcome were still pending at this update. This is not hosted acceptance. Old main-branch run results do not certify this source.

A separate four-changed-file CI preparation batch (five-file review packet including unchanged initializer context) remains uncommitted and pending batch approval. It adds fresh migrated isolated databases, complete test inventory and bounded Linux/Windows execution. The Windows implementation has not yet been run natively.

## Intended first use

This system has never been used at the hotel. Development and acceptance come first, then installation, then guided entry of real historical data. There is no existing live hotel database implied by local synthetic migration/restore tests.

## Intended users and settled scope

Leave operators are HR manager, subordinate assistant and finance manager, with each action restricted by its actual implemented role checks. Do not infer identical powers from inclusion in this list. No employee or department-manager login or employee leave-request workflow is introduced.

Fingerprint exports and department attendance/leave files are reviewed together. Raw punch occurrences and duplicates are preserved. Explicit leave without fingerprint evidence is not automatically a mismatch. Unknown attendance remains unknown. Overtime is entered by day and reviewed against attendance excess; authorized excess requires a reason. Divisors 8/9/12 are hourly-rate divisors, not proof of a roster or worked hours.

Cash leave and overtime use basic salary plus performance bonus only, divided by30 for daily rate. Regular payroll uses actual days in the month. Performance inputs and HR awards are not invented. Manual opening is entered as three categories together and cannot be negative; valid negative debt carry remains a distinct case. Annual new-hire release waits for a complete calendar work month. November/December same-year earned annual remainder is preserved through the following year without accelerating release or creating cash.

These statements describe settled requirements, not a claim that all financial paths are operationally connected.

## What local verification establishes

The current local development source has complete grouped execution of152test files on two freshly migrated synthetic PostgreSQL databases:1684passes27skips at that point. Three later CI-contract cases passed targeted, for1687distinct outcomes, not a fresh whole-suite run. A later focused readiness run passed35cases and skipped one explicitly gated concurrency case, overlapping earlier tests and including six new external regression cases.

HR reconciliation approval has separate actual HTTP/roles/CSRF, two-session, lock timeout/recovery and encrypted nonempty migration0064restore/rollback evidence. Approval records exact reviewed source and decisions with immutable audit. It is neither independent verification that source is true nor permission to post balances, payroll, cash or month close.

The whole one-command CI dispatcher, native Windows parser/runtime, current hosted jobs and target-laptop installation remain unproved. A passing health endpoint proves database connectivity only.

## Supported preparation and safe refusals

- Explicit selected-source read-only reconciliation retains exact opening/date/source provenance, separate ledger view and release/held distinction. It does not establish spendable credit.
- Historical source correction invalidates current approval applicability. Exact historical retry does not rewrite approval or ledger.
- February leap/nonleap clipping and repeated read-only month preparation have synthetic proof.
- A December manual opening reused directly in January is safely refused by the current year-window guard. Continuous cross-year intake is not supported through that route. Attributed late-year preservation is a descriptor, not an operational January carry bridge.
- Read-only preparation of months in reverse order is not approval to post, close, reopen or correct payroll in that order.

## Windows setup boundary

Current scripts require locally installed PostgreSQL and Python, create a virtual environment and install dependencies using pip. Python3.11 is the application-tested choice. Setup tries3.12/3.13 only as documented dependency-resolution fallbacks, not accepted runtimes.

The launcher binds HTTP127.0.0.1:8000 and requires its console window to stay open. Setup creates a shortcut, not a Windows service or automatic startup. It does not set up TLS, hotel LAN/mobile access or three-device synchronization. The draft Inno installer has no native compile/install acceptance. A complete offline first-install dependency bundle is not present in this path.

Backup scheduling is separate from setup. Before relying on it, execute and verify the scheduled tasks, physical destination, encrypted archive, matching PostgreSQL dump/restore binaries and full recovery on the target Windows machine. Preserve the appropriate encryption key file and passphrase for each archive epoch. Local synthetic recovery does not prove Windows-drive or Task Scheduler behavior.

## Acceptance gates before real data

1. Publish only approved batches to ui-overhaul and verify current hosted Linux/Windows/package jobs against exact commit.
2. Complete a native Windows clean install, startup/shutdown, migration, roles and representative import test. Record actual Python/PostgreSQL versions and laptop resources.
3. Build and review attributed cross-year source/transition handling. Keep posting/current availability/year-end/grant/termination/cash disabled until their actual reviewed workflows and effects are accepted.
4. Complete a representative historical multi-month employee/attendance/leave/payroll scenario against agreed manual calculations, including duplicates, negative debt, service interruptions, stale corrections and closed-payroll conflicts.
5. Verify encrypted backup, fresh-machine restore and injected-failure rollback on Windows with physical backup storage and scheduled-task evidence.
6. Execute separately authorized TLS/LAN/mobile/offline-three-device conflict recovery and local-AI latency tests. Core HR/payroll must not depend on model availability.
7. Issue final current-version operator guide and installation package only after these results are recorded. No real employee data before acceptance.

## Decisions still owned by people

No new business decision is required to accept this preparation as a description. Actual HR carry/zero elections and grant windows remain explicit per-employee decisions when performing year close. A concrete historical correction/reopen path across approved payroll needs its own approved workflow. Visual approval or a publication credential never approves financial activation.
