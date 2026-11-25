# Current source and verification status

Instinct, 25 November 2026, 12:44 Africa/Cairo. This page corrects the stale operational index. Historical documents remain available, but their old commit, test and readiness claims are not current acceptance.

## Source of truth

- Repository: [my-business-hotel-hr-payroll-v2-2](https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2).
- Development branch: `ui-overhaul`. `main` is not the development target. The owner controls merging.
- Latest implementation commit at this checkpoint: [8357fa72](https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2/commit/8357fa72da5815ff1c35d6cff2409a168b01563d). This documentation correction is separate from implementation. Before further work, read this page, check the live branch HEAD and working diff, then inspect the exact source body needed for the decision. A path existing is not proof that it is the intended candidate.
- This checkpoint adds the reviewed Item2 daily-overtime implementation and additive migration `0065_overtime_day_review` on top of 1660d473. This is source publication, not proof of deployed schema or hotel-device acceptance.
- No hotel-device installation or acceptance is established. Use synthetic data until acceptance.

## Published changes and their limits

| Source | What changed | Verified boundary |
|---|---|---|
| `d9140fb6aa61a94fa93d0ed1e98751d9c3fb7d62` | Production overtime pay basis and invalidation when its inputs change | Basic plus performance only, divided by 30 and the selected hours divisor. 101 targeted tests passed, including four new migrated PostgreSQL cases. This did not implement daily review. |
| `8357fa72da5815ff1c35d6cff2409a168b01563d` | Scoped report-preset save/delete race correction | Python3.11.18, 62 tests passed: 17 disposable PostgreSQL/authentication/CSRF/concurrency cases plus 45 existing report/role tests. Shared in-scope presets stay shared. This is part of Item4, not its completion. |

The hosted Windows suite on the earlier overtime commit failed. A rendered job summary reported 48 failed, 1640 passed and 26 skipped; only 34 failure entries were exposed in that read. This is not evidence of a green Windows release or proof that every failure is unchanged. No hosted result for 8357fa72 has been qualified in this checkpoint.

## Windows parser source identity, do not combine these artifacts

| Artifact | Identity | State |
|---|---|---|
| Active integrated candidate | `verification/windows_integrated_candidate/launcher.c`, SHA256 `48943a44b1d5ae58bd323805ceef934bb1c844c4e3c6b05ec4ac9d27979d8d7e` | LPAC opt-out policy attribute present. Suspended child creation and creation-time Job list already present. Still a verification candidate, not a working installed Windows parser. |
| Separate ordinary AppContainer measurement | `verification/ordinary_ac1116/REVIEWED.tar.gz`, committed by `934574b98839aa24c4cb8eed12639d85f1efa2ee`; SHA256 `a09df511f1b1fdc1267b87d4c5a017cb0c3408aecbfddd89abc8eef517cd4682` | Launcher inside the archive omits LPAC opt-out; it is not the launcher at the path above. The archive and `.github/workflows/ordinary-ac1116.yml` were published for one finite measurement, not adopted as product code. |

The [ordinary measurement run](https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2/actions/runs/45585109638) failed source preflight: a depth-one checkout lacked the explicitly checked `90f970b` historical object. Dependency setup, compilation, profile creation, child startup and probes were not reached. This is not a parser startup result. No automatic retry is authorized by this record.

A later portable consultation prompt described the ordinary candidate while pointing to the LPAC source path. That was a source-reference error, not proof that the development branch had reverted. The original full consultation input separately labelled both candidates. Stale operational docs did not provide the required clear current index. Git logs and scattered experiment notes do not replace maintaining this document.

Other runtime/host-runner proposals remain separate unadopted proposals. Do not infer their presence, test results or approval from another artifact's name. No new native execution is authorized by documentation.

## Item2 daily overtime source checkpoint

Current local work prepares manual same-day review, explicit confirmation, append-only correction and explicit monthly aggregation. It preserves finance payroll access and denies assistant payroll access; it does not grant finance leave-ledger writes. The owner chose stacked review details and monthly summation of unrounded daily values, rounding once at monthly money storage. Divisors8/9/12 determine the hourly rate, not a roster. No automatic award is intended.

- Backend review accepted bounded stages, not all of Item2. Daily GET shared read/restore gate, source freshness at approval/close, frozen payment proof, mixed paid/unpaid handling and predecessor binding have targeted tests.
- Actual browser previews in Arabic, English and Chinese were inspected at desktop and390px mobile widths, using synthetic data. This is not native Windows or hotel-device acceptance.
- Final developer qualification: 1819 collected nodes across 158 files, executed as five disjoint completed runs on Python3.11.18: 481 passed/9 skipped, 475/1, 409/11, 241/21 and 169/2. Deduplicated result: 1775 passed, 44 skipped, zero failures. This was not a single full-suite run. Fixtures include migrated PostgreSQL and legacy SQLite, not1775 PostgreSQL cases. Prior focused counts overlap and are not added.
- Dedicated PostgreSQL names remain enforced. Baseline8357 on fresh0064 had13 name-guard failures (nine leave, four return-cycle); current fresh0065 with the correct separate synthetic_leave and synthetic_cycle fixtures clears them. Two earlier downgrade failures came from a stale disposable0065 schema; rebuilt exact migration tests pass. No fixture guard was weakened.
- The44 skipped nodes retain explicit dedicated acceptance flags, committed synthetic fixture/client requirements and SQLite-only concurrency limitations. Exact execution IDs, JUnit, source hashes and skip reasons are retained in qualification artifact SHA256 `0fd68a1936c3d9733a7fabeccbcd7427b3ba8f78e4316d9205d8a8c8bd3fb44d`. Four generated fingerprint parameter display IDs contain ZIP timestamps; actual execution IDs are retained, and collection comparison normalizes only those display IDs.
- Paid-legacy correction is fixed: partly paid historical monthly overtime remains frozen through reopen/reapprove/reclose and later installments, including append-only payment corrections. Unpaid monthly-only first-payment bypass remains refused. Actual PostgreSQL chain and authenticated/CSRF HTTP route tests passed. Superseded payment records still establish historical-paid status, while active records alone determine payment totals.
- Independent exact source/artifact review accepted the bounded backend and reconciled test accounting. It did not execute these tests independently. No whole-system, Windows, native or hotel-device acceptance follows from it.
- Daily-specific simulation and AI execution/snapshot tests exist; the AI advisory-before-table lock change has a concurrent PostgreSQL regression. Whole-system concurrency is not certified by one tested ordering.

## Historical archive and recovery gap

On23November, four unused fixed-January leave-preview files were removed from the active work after a byte-exact local archive:

- `app/leave_annual_close_preview.py`
- `app/leave_off_day_close_preview.py`
- `tests/test_leave_annual_close_preview.py`
- `tests/test_leave_off_day_close_preview.py`

Original local branch `مهملات` pointed to `f7074872b64e81a0037cfe94e1ad79059551f88b`, based on `2f3d3a61f15500e0c0ab3d56509a5120af4b7322`. A bundle was verified then. A separate four-file recovery copy at `1247309c510c248468eb6c732bf48679425a9614` was also independently restored and checked then. That copy is not replacement ancestry for the original archive.

Current recovery status: the live repository has no `مهملات` branch. Neither the historical local checkout nor its bundle is present in the current checkout. Recovering the original transferred bundle is open. Historical successful recovery checks are not a claim that the bytes are currently available. Do not fabricate or replace archive history. These four leave previews are unrelated to the later Windows experiments, and their paths remain absent from the active tree. Still-used code and migrations were not deemed disposable.

## Next checkpoints

1. Recover the historical archive attachment and verify its real contents, prerequisites and recoverability.
2. Run remaining explicit acceptance fixtures and qualify installation/native/hotel-device behavior separately. The reviewed daily-overtime source checkpoint does not complete all of Item2 or the wider branch.
3. Keep Item3 and remaining Item4 proposals separate until qualified and approved. No blanket completion.
4. Qualify the narrow local-AI proxy/redirect fix separately. Local-canary findings do not establish an observed real-use disclosure; localhost resolution check/connect remains a stated limitation, not pinned transport.
5. Update this index with each implemented or tested boundary, including exact commit/artifact, outcome, limits and next step. Before every handoff, inspect the exact referenced source body and compare candidate identities.
