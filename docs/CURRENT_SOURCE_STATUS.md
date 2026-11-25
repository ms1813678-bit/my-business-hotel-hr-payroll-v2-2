# Current source and verification status

Instinct, 25 November 2026, 16:37 Africa/Cairo. This page corrects the stale operational index. Historical documents remain available, but their old commit, test and readiness claims are not current acceptance.

## Source of truth

- Consult and maintain [the project decision notebook](PROJECT_DECISIONS_NOTEBOOK_AR.md). Preserved historical implementation reports are not new user decisions or current technical acceptance. Three notebook corrections were applied at16:59 on25November: recovery codes never in chat, negative manual opening balances rejected, UID1687 thirteen isolated PG model tests with migration/DB immutability still absent.

- Repository: [my-business-hotel-hr-payroll-v2-2](https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2).
- Development branch: `ui-overhaul`. `main` is not the development target. The owner controls merging.
- Published baseline before this monthly-leave checkpoint: `0c16238161e6b022e1b40c5eae21a506618fa93f` (strict local-model JSON request format), after `42d4e982f31599721458ea15abcb695e47721046` (recovery fixture seam). This document is part of the subsequent monthly-leave commit. Check the live `ui-overhaul` HEAD and working diff before further work. A path existing is not proof that it is the intended candidate.
- Published schema remains `0065_overtime_day_review`. The later internal0066 attendance schedule assertion candidate is isolated and not integrated or published. Published source is not proof of deployed schema or hotel-device acceptance.
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

Published source implements manual same-day review, explicit confirmation, append-only correction and explicit monthly aggregation. It preserves finance payroll access and denies assistant payroll access; it does not grant finance leave-ledger writes. The owner chose stacked review details and monthly summation of unrounded daily values, rounding once at monthly money storage. Divisors8/9/12 determine the hourly rate, not a roster. No automatic award is intended.

- Backend review accepted bounded stages, not all of Item2. Daily GET shared read/restore gate, source freshness at approval/close, frozen payment proof, mixed paid/unpaid handling and predecessor binding have targeted tests.
- Actual browser previews in Arabic, English and Chinese were inspected at desktop and390px mobile widths, using synthetic data. This is not native Windows or hotel-device acceptance.
- Final developer qualification: 1819 collected nodes across 158 files, executed as five disjoint completed runs on Python3.11.18: 481 passed/9 skipped, 475/1, 409/11, 241/21 and 169/2. Deduplicated result: 1775 passed, 44 skipped, zero failures. This was not a single full-suite run. Fixtures include migrated PostgreSQL and legacy SQLite, not1775 PostgreSQL cases. Prior focused counts overlap and are not added.
- Dedicated PostgreSQL names remain enforced. Baseline8357 on fresh0064 had13 name-guard failures (nine leave, four return-cycle); current fresh0065 with the correct separate synthetic_leave and synthetic_cycle fixtures clears them. Two earlier downgrade failures came from a stale disposable0065 schema; rebuilt exact migration tests pass. No fixture guard was weakened.
- The44 skipped nodes retain explicit dedicated acceptance flags, committed synthetic fixture/client requirements and SQLite-only concurrency limitations. Exact execution IDs, JUnit, source hashes and skip reasons are retained in qualification artifact SHA256 `0fd68a1936c3d9733a7fabeccbcd7427b3ba8f78e4316d9205d8a8c8bd3fb44d`. Four generated fingerprint parameter display IDs contain ZIP timestamps; actual execution IDs are retained, and collection comparison normalizes only those display IDs.
- Paid-legacy correction is fixed: partly paid historical monthly overtime remains frozen through reopen/reapprove/reclose and later installments, including append-only payment corrections. Unpaid monthly-only first-payment bypass remains refused. Actual PostgreSQL chain and authenticated/CSRF HTTP route tests passed. Superseded payment records still establish historical-paid status, while active records alone determine payment totals.
- Independent exact source/artifact review accepted the bounded backend and reconciled test accounting. It did not execute these tests independently. No whole-system, Windows, native or hotel-device acceptance follows from it.
- Daily-specific simulation and AI execution/snapshot tests exist; the AI advisory-before-table lock change has a concurrent PostgreSQL regression. Whole-system concurrency is not certified by one tested ordering.

## Published one-use disaster recovery login proof

[a9fb08b1](https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2/commit/a9fb08b13e93abeb37ede96b3b7461a8208d1a89) adds the owner-approved bounded newer-record proof. After an encrypted restore into an accountless target, the exact validated recovery record that opened the selected archive can prove possession once. The existing account password or eligible recovery-code path, sensitive-action throttle, forced password changes and recovery-code regeneration remain. Normal setup creates an administrator; restore with that existing account uses current-credential carry-over and archive-only account locks, not the accountless proof branch.

- Active backup encryption still returns to the archive-era record as documented. The new proof does not automatically rewrap it to the newer phrase. The stored archive hash is provenance and format-checked, not rechecked at sign-in; the enforced live binding is the restore epoch plus wrapped-record and key digests.
- No new route, admission layer, lock or migration. A present corrupt proof fails closed; an absent proof preserves the legacy DB-record phrase check. New proof stores a bounded public wrapped record and digests, never the supplied phrase or raw key. The wrapped record remains an offline phrase verifier and is not harmless data.
- Exact integrated-source developer qualification:77 targeted tests passed,2 warnings in17.92s, mostly SQLite and in-process handler fixtures. One separate dedicated PostgreSQL source/target encrypted restore and distinct-key forced rollback test passed in8.40s. Before/after hashes matched for the10 intended files. All10 published GitHub blobs matched the committed local blobs; main remained936f5d62.
- Independent focused review inspected source and hashes and found no concrete defect in that scope. It did not run the77 tests or the PostgreSQL test. Neither route fixtures nor the helper proof establish live HTTP admission concurrency, Windows, external-media, native or hotel-device acceptance.
- Documentation now states the configured target encryption prerequisite for an encrypted pre-restore snapshot, source recovery-record preservation and the absence of a supported encryption-reset command. No unsupported technician-reset route is promised.

## Isolated attendance assertion slice, not published

Candidate1642 is preserved separately from the published branch. It adds an internal two-step explicit operator schedule assertion and withdrawal with immutable history, source/projection checks and0066 deferred constraints. It does not add assignment UI or HTTP, infer a roster from punches/divisors, alter approved endpoints or automatically reverse money. Source changes stale daily overtime proof until explicit replacement and monthly restaging; approved/closed/paid states remain protected.

- Final targeted candidate suite:80 passed,1 skipped,32 warnings. Dedicated same-receipt/different-receipt and legacy collision races and real encrypted restore/rollback were separate runs, not added to that count. The restore compared five business tables after a discriminating post-backup withdrawal and checked trigger persistence. The actual pre0066 upgrade artifact covered one committed attendance-consumption digest, not every historical financial artifact.
- Direct corruption tests forced deferred constraints to evaluate. They are not a claim that an invalid final COMMIT was executed. Absolute-date raw CSV overnight consumption/assertion/approval is covered; the legacy out<=in correction branch and original hotel BIFF8 source/device cycle are not established.
- Integration remains held on the separate HTTP/admission dependency. The specifically held experiment was not repeated or replaced. Bounded read-only lock inventory is not global proof of no deadlocks. Any new visible assignment UI still needs preview and owner choice before integration.

## Item6 narrow local-model transport checkpoint

The reviewed ai_assistant.py-only patch disables environment/registry proxies, refuses redirects and checks http/https plus all resolved loopback addresses before opening. It adds no HTTPS restriction, transport framework or machine network configuration. Exact source diff SHA256: `64129874ba2d4533fe511bdd8c20d9f493abfc91878e408f5fa2681b787b74df` (27 insertions,6 deletions).

Python3.11.18 qualification in a separate disposable copy:68 passed,2 deprecation warnings across seven local canaries and existing AI/gateway/UI/HR/i18n tests. Before the patch, the same seven canaries had5 failures and2 passes. POST307 already failed closed before this patch. No observed real-use disclosure is established by synthetic canaries.

Residual: localhost name resolution is checked before a later separate connection lookup. Addresses are not pinned. This checkpoint is not absolute egress closure, full Item6 acceptance or native/hotel-device qualification. Further Item6 requirements and acceptance remain separate.

## Historical archive and recovery gap

On23November, four unused fixed-January leave-preview files were removed from the active work after a byte-exact local archive:

- `app/leave_annual_close_preview.py`
- `app/leave_off_day_close_preview.py`
- `tests/test_leave_annual_close_preview.py`
- `tests/test_leave_off_day_close_preview.py`

Original local branch `مهملات` pointed to `f7074872b64e81a0037cfe94e1ad79059551f88b`, based on `2f3d3a61f15500e0c0ab3d56509a5120af4b7322`. A bundle was verified then. A separate four-file recovery copy at `1247309c510c248468eb6c732bf48679425a9614` was also independently restored and checked then. That copy is not replacement ancestry for the original archive.

Current recovery status: the live repository has no `مهملات` branch. Neither the historical local checkout nor its bundle is present in the current checkout. Recovering the original transferred bundle is open. Historical successful recovery checks are not a claim that the bytes are currently available. Do not fabricate or replace archive history. These four leave previews are unrelated to the later Windows experiments, and their paths remain absent from the active tree. Still-used code and migrations were not deemed disposable.

## Next checkpoints

1. Continue independent checklist requirements and resolve current concrete blockers without restarting specifically held HTTP/admission experiments. The isolated0066 assignment slice stays separate until its integration dependency is cleared.
2. Monthly leave choices were settled on25November: all statuses separately and historical employees with current active/left status. The monthly PDF implementation below is bounded, not whole Item2/4 acceptance. Preserve custom reports and approved financial filters. New visible assignment UI remains separate from the held0066 integration.
3. Run remaining explicit acceptance fixtures and qualify installation/native/hotel-device behavior separately. Recovery publication and isolated attendance proof do not complete all of Item2 or the wider branch. Keep Item3, remaining Item4 and broader Item6 boundaries explicit.
4. Retain historical archive recovery as an unclosed evidence gap: recover the actual transferred archive and verify contents/prerequisites when its source becomes available. It is not a mandatory first step and must not stall independent work or be replaced with fabricated ancestry.
5. Update this index with each implemented or tested boundary, including exact commit/artifact, outcome, limits and next step. Before every handoff, inspect the exact referenced source body and compare candidate identities.

## Monthly leave checkpoint, 25November 2026

Monthly-only leave PDF changes use all confirmed/draft/cancelled states separately and include employees with leave overlapping the report period, with current active/left or localized unknown status. The assistant aggregate contains no employee identities and splits each type/status. Calendar days are clipped to each month; recorded charge days appear only in the leave-start month and are explicitly not executed deductions. Custom report defaults, payroll/attendance approval filters and schema remain unchanged.

AR/EN/ZH synthetic detailed pages for all three states plus aggregate pages were rendered and inspected. Stored localized leave-type labels are used; missing translations show a localized marker plus type ID, never silent Arabic fallback. Raw employee names remain unchanged. The Chinese monthly path embeds unmodified installed-distribution DroidSansFallbackFull.ttf with Apache2 license/copyright notice and explicit non-CJK fallback, including footer digits. The source asset is about3.9MiB; binary provenance is the installed distribution, not independently network-verified upstream. Existing recursive app packaging includes the asset. Custom Chinese PDF font portability is unchanged.

Independent static review covered exact source, font/license and delta scope; it did not run the developer tests or inspect pixels. Developer qualification on the exact integrated source:64passed,2warnings in4.38s across new monthly-leave and existing monthly/custom-builder tests. Synthetic SQLite fixtures run with the configured migrated PostgreSQL suite guard intact, not a claim that every case ran on PostgreSQL. Full-table snapshots after six PDF variants show no data changes. This does not establish native Windows, hotel-device or full Item2/4 completion. The separate0066 attendance assertion, HTTP/admission experiment and native adapter holds remain unchanged.

Latest-source/cleanup check before integration: remote and local development HEAD both0c162381, main936f5d62 unchanged. All15tracked verification directories have workflow/build or current-document references. All127top-level app modules and13script files have recorded references. No tracked cache/log/tmp/bak files were found, and no file was proven obsolete for removal. No files were moved or deleted. This bounded audit does not certify all code is needed; ambiguous historical material and active evidence were preserved. The historical missing مهملات archive gap above remains unresolved.

## Shared leave recording service checkpoint,25November2026

Owner instructions from23November21:37 and reaffirmed25November16:43 remove the leave-request premise. Employee/department-manager submission is not part of this system. The request-only72h gate is removed from operator leave recording, including historical entries. No new submission route or permission is introduced. HTTP and AI draft creation use one service while preserving their audit actions, caller transactions and existing permissions. Confirm/cancel and balance effects are unchanged.

The service locks the employee row before checking non-cancelled overlap, so cooperating HTTP and AI creates cannot both commit an overlapping record. The persistent PostgreSQL regression exercises both directions, same-entry cases, first-writer rollback and existing AI gateway table-lock ordering. It observes an actual backend Lock wait with a bounded poll. Removing the employee row lock fails the negative control; restoring it passes. These are actual Base tables in disposable schemas, not production/migrated-schema acceptance or global deadlock proof. Direct execute_plan proof is not a full AI HTTP approval test. Existing AI gateway table-lock ordering is separately inspected. UID/offline command integration remains separate and has no schema migration here.

The employee row uses FOR NO KEY UPDATE after alphabetical ROW EXCLUSIVE intents on audit_logs/employees/leave_records, preserving compatibility with unrelated FK KEY SHARE checks and avoiding a row-to-table inversion against the existing AI gateway table locks. The application has no isolation override; the PostgreSQL regression requires READ COMMITTED. Existing SHARE readers/importers may delay the route, which has no new lock timeout. Only current main/AI LeaveRecord creators are covered, not arbitrary raw SQL writers. No UID model or migration is included; isolated UID groundwork remains separate.

Final integrated developer qualification on2887068 base:72passed,zero skipped,2warnings in18.93s across test_leave_create_pg, test_leave_draft_service, test_leave_routes, test_ai_gateway and test_ai_plan_model_output. The actual PostgreSQL concurrency regression explicitly PASSED, not skipped by database-name/xdist guards. The configured migrated PostgreSQL guard remained unchanged; its configured fixture DB is separate from the unique per-test Base schemas. PostgreSQL was stopped after cleanup.
