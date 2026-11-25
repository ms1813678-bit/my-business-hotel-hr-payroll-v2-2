# Current source and verification status

Instinct, 25 November 2026, 17:07 Africa/Cairo. This page corrects the stale operational index. Historical documents remain available, but their old commit, test and readiness claims are not current acceptance.

## Source of truth

- Consult and maintain [the project decision notebook](PROJECT_DECISIONS_NOTEBOOK_AR.md). Preserved historical implementation reports are not new user decisions or current technical acceptance. Three notebook corrections were applied at16:59 on25November: recovery codes never in chat, negative manual opening balances rejected, UID1687 thirteen isolated PG model tests with migration/DB immutability still absent.

- Repository: [my-business-hotel-hr-payroll-v2-2](https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2).
- Development branch: `ui-overhaul`. `main` is not the development target. The owner controls merging.
- Published baseline entering this documentation correction: `d648425744f7603e0d0f2c15699163111040ec24` (maintained decision notebook), following `7f868c5869da3dcd57ff43d8b8fb623813b65396` (shared operator leave recording), `2887068c2d77c02327b92533ef6b16c2ed008b55` (monthly leave reporting), `0c16238161e6b022e1b40c5eae21a506618fa93f` (strict local-model JSON request format) and `42d4e982f31599721458ea15abcb695e47721046` (recovery fixture seam). This correction changes documentation only. Check live development HEAD and working diff before further work; a path existing is not proof of candidate identity.
- Published schema after the UID source checkpoint below is `0067_sync_uid_base_entities`, directly following `0065_overtime_day_review`. The later internal0066 attendance schedule assertion candidate is isolated and not integrated or published. Published source is not proof of deployed schema or hotel-device acceptance.
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

## Current-entry documentation correction,25November17:07

Correct README daily-overtime status to the bounded published implementation described above, identify old CI evidence as historical and refresh this entrypoint publication chain. Decision notebook remains canonical for owner decisions and source gaps. Documentation/link checks only, no product/schema/permission change or new test/CI/native/hotel acceptance claim. Isolated UID migration preparation is still underway, not integrated.

## Monthly exact charged totals,25November17:21

Source-qualified external static review exposed float detail totals (1.1+2.2 becoming3.3000000000000003) while monthly aggregate remained Decimal. Monthly detail and all-status aggregate now use existing exact bounded leave arithmetic, independent of ambient Decimal precision/traps. Custom detail and default aggregate behavior remain unchanged. No ledger/payroll change, unknown-status fallback, role expansion or UI redesign. Targeted developer run:79passed,zero skipped,2warnings in7.22s across monthly choices/monthly report/custom builder/leave routes with migrated configured PostgreSQL guard intact; actual report/route data fixtures are SQLite, not79PGcases. Negative control restoring old float detail sum fails the new valid-fraction regression. Six AR/EN/ZH synthetic PDFs generated; all12pages inspected for layout. Existing PDF2dp/float formatting and Excel conversion remain unchanged, so exactness here concerns report data totals only. Independent source/helper review accepted the bounded fix; it did not execute the79test suite. No hotel-device/native/wholegroup acceptance.

## Local AI input/approval boundary,25November17:29

Bounded D1-D6 repairs: non-string action; missing actual unguarded executor keys; whitespace required values; null edit preview/execution alignment; fingerprint textual identity normalization; malformed receipt rejection before database lookup; controlled OSError and malformed command JSON; requested-language model status. Existing optional defaults, business permissions, human review/approval and transaction ownership remain. Fingerprint model is String50, form strips input, AI blank remains refused/null unchanged.

Exactcandidate qualification222passed,4skipped,18warnings30.49s under configured migrated PostgreSQL guard. Fixtures include SQLite and actual migrated PostgreSQL isolated schemas. Four skips are SQLite variants of PG-only lock/concurrent approval/process-death/timeout cases; PG variants executed. RealPG/SQLite identifier preview/write parity, authenticatedCSRF command/QA timeout controlled output and receipt actor/token/hash checks pass. Independent static source review accepted; it did not rerun220.

Not a real-model or wholeItem6 result. DNS check-to-connect gap, deeper mixed-language route error prefixes and non-OSError HTTP protocol failures such as IncompleteRead remain outside this patch. No blanket robust-transport, absolute egress, UI language-purity, Windows or hotel-device acceptance claim. Legacy unused ask_local_model_json Arabic errors remain unchanged.

Final shared-tree qualification on6fa5924 base:222passed,4explicitSQLite-onlyskips,18warnings31.71s; same11files and migratedguard. PostgreSQL stopped after disposable-schema cleanup.

## UID foundation,25November17:49

Additive immutable sync_uid for departments/employees/leave_types/leave_records, preserving integer IDs/FKs/codes/business history. Real0065 backfill, defaults, canonical constraints, raw/bulk immutability, migration-failure rollback, populated downgrade refusal and clean-empty downgrade are covered. Source lineage is single0067->0065; held0066 remains absent and would require explicit rebase if later adopted.

Final current-source Python3.11/PG16.2 targeted selection417passed6skipped100warnings106.51s, not fullsuite. Two dedicated committed reconciliation race/timeout tests skipped; four SQLite variants of PG-only AI tests skipped, PG counterparts ran. Actual populated linked4entity+audit dump/restore preserved values/IDs/UIDs and8constraints/4enabledtriggers/2functions/4defaults; restored enforcement and guarded health passed. Independent exact-source review accepted, without suite rerun. Guard probes roll back rows but may consume sequence values. Test-only destructive downgrade helper is name-gated, not strong target isolation.

Before any installation upgrade: stop writes, verified pre-upgrade full backup and tested restore, maintenance window for DDL locks. Ordinary downgrade refuses seeded rows; rollback uses the retained backup. SQL-generated IDs are not CSPRNG/authentication secrets. Read SYNC_UID_CONTRACT release procedure. No applier/network/disclosure policy, full encrypted restoreepoch/admission, native Windows or hotel-device acceptance.

Final shared-tree integration run:417passed6skipped100warnings108.45s on the same tested product/test bytes. No hosted-CI or deployed-device result.

## Local AI localized refusal and pinned destination,25November18:05

Source-scoped D8 error display keeps actionable known business refusals in AR/EN/ZH, exact finite authored catalog plus exact domain reasons/bounded fullmatch parameters. Unknown/internal text is generic, including forged localized markers, wrong-language/class/args. Existing role/CSRF/review receipts/actor binding/business rules remain. HTTPException/IncompleteRead/RemoteDisconnected and malformed local-model bodies now take controlled error/fallback paths. No blanket whole-UI language or native-linguist claim.

Each local-model status/POST request resolves once, validates every result loopback and dials only that numeric list. Original HTTPS hostname/SNI and default certificate/hostname verification are retained; proxy and redirect refusal stay. IPv6 socket-creation failure may fall back only to another verified address. HTTPS resolver default443 now matches preexisting urllib actual443; model default11434 unchanged. Timeout remains per address, not an overall request deadline; body size is not newly bounded. No absolute global-egress, real-Ollama or Windows acceptance claim.

Composed current7654222 qualification:526passed4skipped57warnings74.90s, mixed SQLite/migratedPG with reali18n/guard. Four SQLite PG-only AI variants skipped, PG counterparts executed. Seven added fullymigratedPG authenticated/CSRF TestClient cases cover actual domain-refusal display3langs/no partialwrite, successonce/replay/actorbinding3langs and CSRF/finance role denial, no auth/CSRF bypass. Temporary generated TLS certificates exercise strict good/bad hostname, IPv4/IPv6, status/POST rebinding, fallback and cleanup. Oldtransport negative control10failed3passed before finalsocketcreation case was added; no additive counts. Independent exact-source/static reviews accepted bounded D8/pin scopes; did not rerun full selection. AR/EN/ZH refusal-page pixels inspected, readable/unclipped/no execution form, HTML escaping assertion passes. No actual model, whole Item6/native/hotel acceptance.

Final shared-tree composed confirmation:526passed4explicitSQLite-onlyskips57warnings75.01s; exact14filepublication. Disposable d8_http/sync_uid_m/sync_uid_t schemas checked absent after teardown.


## Literal employee selection and truthful bounded reports,25November18:16

Employee name queries preserve literal substring matching with explicit ILIKE escaping for backslash, percent and underscore. Exact normalized employee codes remain first. Name results are deterministically ordered by full name and integer ID, fetch six and keep five with explicit overflow metadata. Employee/payroll/attendance/leave/advance/staff reports warn in AR/EN/ZH before partial results; the compatibility list helper remains. Explicit unmatched individual attendance requests never widen to the register. General plural and separately quantified singular register questions stay unfiltered. Failed text/code matching asks for the employee code or name alone rather than claiming the person does not exist. Smart-scan labels now describe leave drafts and pending-or-approved advances; count predicates and permissions are unchanged.

Final shared-tree targeted developer qualification on c38c159:582passed4skipped77warnings87.36s, mixed SQLite and actual migrated PostgreSQL. Four SQLite variants of PG-only AI tests skipped; PostgreSQL counterparts ran. Twenty new migrated-PG cases cover literal escape execution, code precedence, deterministic overflow, all six report families, general attendance and unresolved individual/time-tail handling. No business writes, role/schema change, new employee submission, real model or whole-project claim. Independent static review accepted exact four source/test hashes and final refinement without rerunning tests. Actual AR/EN/ZH template pixels inspected: warnings precede five rows and labels/layout are readable, not native-language acceptance.

Known parsing limits remain: no Arabic letter normalization; extra time words remain part of literal name text, with an honest failed-match suggestion. Attached quantifier prefixes are not generally parsed. Mixed general and individual phrases use the first recognized employee phrase unless a code exists. Use a code for exact targeting. This is not a natural-language parser expansion, hotel-device acceptance, hosted-CI result or whole-UI language guarantee.


## Monthly history and evidence-truth correction,25November18:37

The monthly wrapper now includes active and former employees with actual period records in payroll/attendance/advance/staff detail sections and shows their current employment status, preserving approved-only payroll/attendance and existing request/action dates and states. No current-active-only custom-report default or column changes. The ordinary archive route ends an open service period before setting archived; Left describes current normal-path service state, not a reconstructed departure date.

Approved-only monthly attendance no longer claims unresolved days are zero; it explicitly does not measure them. Already computed payroll/attendance comparisons render; selected unsupported/missing comparisons have explicit notes. Payroll summary distinguishes absent approved/closed runs from actual zero; missing current detail items do not create zero-based comparisons. Attendance charts state employee-day count and no completeness proof, with missing-payroll absence gaps distinct from zero. Known translations for these notes added; the like-for-like table is kept together.

Final exact shared-tree targeted run:200passed0skipped3warnings34.01s, mixed SQLite and actual fullymigratedPG0067. The new migratedPG case renders three languages and proves unchanged employees/runs/items/attendance/audit row JSON plus no pending ORM writes; disposable schema cleanup checked. Old-source negative control8failed2passed before later content additions. Independent static exact-source and supplement reviews accepted without suite rerun. Nine synthetic PDFs generated; ten history/missing pages plus three attendance-chart caption pages visually inspected, including intact English like-for-like block on its own page. A narrow English header wraps awkwardly but loses no text. Not a whole11-option, native-language, hostedCI, Windows or hotel-device acceptance.

All bounded audit leads and dispositions persist in MONTHLY_REPORT_FINDINGS_1697.md. Advance/request-versus-installment date, action creation-versus-payroll month, snapshot/current department grouping, existing attendance-role payroll-derived non-money access and chart presentation leads are not silently redefined. Ordinary payroll creation uses calendar-month bounds; arbitrary historic non-calendar data remains unqualified. Extreme date overflow is explicitly tracked for a separate bounded correction. No role/schema/business-write/approval change.


## Existing login gate qualification and backup protection limits,25November18:42

Normal POST /login already takes the exclusive PostgreSQL credential/restore advisory transaction lock before checking the counter and authenticating. The alleged missing helper row-lock is therefore not proof of an HTTP failure-counter bypass. Two added fullymigratedPG0067 actualHTTP/CSRF tests observe the second backend Lock/advisory wait before authenticate, using absent and one-failure initial states. They establish two counted failures/one audit/persisted near-two-hour lock and correct-password refusal while locked, not every success/restore/recovery/helper interleaving. Product behavior and global counter scope are unchanged. Source observation: the standalone helper has no own lock; do not reuse it concurrently without understanding the caller gate.

Final shared-tree37passed0skipped4warnings11.75s on Python3.11.18/PostgreSQL16.2 READ COMMITTED. Independent exact source/test static review accepted without test rerun. An uncommitted gate-disabled counterfactual fails both wait assertions; no lost-update execution claim. Disposable schemas checked absent. See LOGIN_CONCURRENCY_1698.md.

BACKUP_PROTECTION_LIMITS.md records the accurate archive/live-device boundary. The live DB retains its working encryption key for unattended backups; exported records strip it. Archive/passphrase protection is not live-database or whole-device protection. Current SYSTEM scheduled code paths need effective trust/ACL verification before installation acceptance. Actual Windows/hotel ACLs and task configuration remain unknown. A synthetic preflight model is a design proposal only; no classifier, ACL/task-account/crypto change is adopted. No new native/device/security completeness claim.


## XLSX formula inference fixed,25November18:46

Only the HR/custom export final serialization changes: formula-typed cells are stored as literal text with values unchanged, after all sheet/translation construction. No intentional formula is emitted by either writer. Numeric/date/bool values, DB data, calculations, roles and selections are unchanged. Nine new tests plus focused reports/report-builder/monthly/manual-CLI/roles/route-security:finalshared155passed0skipped2warnings30.58s. Originalworker candidate retained unchanged and independently statically accepted against5a3b62d. Own negative control6fail3pass; actual migratedPG0067 sixAR/EN/ZHworkbooks0formulas/preservedliterals/numerics/date and schema cleanup. Own+main inspected LibreOffice7.3.7.2 literal text pixels, not Microsoft Excel/native/hotel proof. Existing long-text clipping/print paging not fixed. See XLSX_FORMULA_SAFETY_1699.md and sanitized MUSE_RECONCILIATION_RECORD.md for bounded historical review dispositions. Earlier Muse delta archives stale; minimal refreshed handoff required.


## Internal UID snapshot1701,25November18:53

Add explicit-field, one-UID SELECT-only internal snapshot over four existing0067entities, parentintegerFKs joinedtoUIDs in samequery. No defaultallfields/relationship/PKdata/branchref fallback; malformed/unknown identity and pending ORMnew/dirty/deleted state refuse without autoflush. Actual migratedPG16newcases+12existingUIDmodels:finalshared28pass0skip29warnings13.77s; exact2hashindependentstaticaccepted. Row/auditvaluesbefore/afterequal,SELECTinstrumentation,emptyidentitymap,crossschemaUIDscope,DBREADONLYproof; negativeguardremoval3failboundedexperiment. This helper is not an authorization boundary: caller can select sensitive scalar fields and must establish administrator authority. Earlier flushed/CoreDML not detected; caller owns READONLY and isolation. No version/history/merge receipt/crosscallatomicity/wire/API/device/channel/write integration. Existing isolatedsyncpoliciesnotadopted. See SYNC_SNAPSHOT_1701.md.


## Canonical typed snapshot codec1702,25November19:12

Integrate Muse-origin isolated stdlib codec with credited Instinct controlled-number/subsecond-datetime repairs and17regressions. No callers/store/network/auth/journal activation. Version1 canonical typed JSON preserves explicit scalar types/Decimalscale/offsets/Unicode and rejects malformed/noncanonical/oversized values under documented resource bounds. Original78passonoursupportedPython3.11;adversarialoldsource8fail9pass;refined95pass;finalsharedcodec+actualPGsnapshot/UID123pass0skip29warnings13.32s. Independentexacthashstatic+3.10probes accepted, notindependentpytest. SHA256notpermission;decodeisnotfieldauthority. Encodeallocation/customtzinforesiduals documented in SNAPSHOT_CODEC_1702.md. Historicalbroadreviewpartialsourcecoveragekeptseparate. Departmentjournal1703experimentnotadopted;sameDBroleactorcontextspoofacceptedcounterexamplepreservedinternally,notsecurityguarantee.


## Monthly date preflight1706,25November19:29

Investigated retained1697extreme-date lead: exercised old monthly paths alreadyreturned400,not500, but Python year0/year10000 detail was untranslated and validation happened inside report DB context. Shared option-specific arithmetic preflight now refuses before DB/render/save with existing AR/EN/ZH invalid-month reason. No arbitrary businessyear cutoff: earlyyearleave remainsallowed; payrollFeb0001/chartJune0001/latestNov9999 produce actualHTTPPDF with correct saved bounds. Candidate180passed0skipped2warnings60.58s,44newboundarycases; existingfixtureSQLite qualifications retained, notallPGbehavior. No customreport/role/datebasis/layout/device claim. Independent static review accepted against b613; reviewer did not run tests. Final shared result below. See MONTHLY_DATE_BOUNDS_1706.md.

Final shared-tree qualification: 180 passed, zero skipped, two warnings, 32.42 seconds; 44 new boundary tests including nine preserved modifier-only rejection cases. Independent acceptance was static source review, not test execution. No visual/layout acceptance claim.


## Actual HTTP auth queue repair1709andCI1707,25November19:47

Actualmiddleware+signedsession+auth+queuedexclusivePGproof now refuses secondsharedlock503RetryAfter5, releasingfirstholderandallowingexclusiveprogress; samecookiesucceedsafterrelease. Noexemptions/auth/epochbypass/journalactivation. Minimalsharedhelpertrylockreplacesblockingacquisition. Arabicbareindefinitecountformsrepair7observedproductCIregressions;definitenamesandcodesneverbroaden;2oldwordingtestsupdated;UIDdump/restoreremovesdev-onlypgserverrequirementusingexistingfinder. Exactcombined222pass0skip29warnings60.58s, independentstaticreviewnotrerun. LatestWindows2deemigrationcommandimmediate-1073741502failurehasnoPythoncauseevidenceandisnotfixedbythispatch. See AUTH_GATE_AND_CI_1709.md.

The helper message is neutral and translated AR/EN/ZH: other credential writers share the advisory key, so contention alone does not prove a restore is active. Immediate503 is intentional, no polling added. The actual HTTP race runs in all three languages.

Final supported Python3.11.18 shared verification after neutral AR/EN/ZH message:222passed0skipped29warnings60.58seconds. Static source acceptance received; no independent test rerun.

## Manual opening application1708,25November20:35

The existing review page now applies an explicitly confirmed A/W/H opening together with immutable source-bound evidence, zero-category aggregate coverage and safe retry. Migration0068 distinguishes this baseline from earned monthly credit; canonicalW/H become balance tracked without history backfill. Reconciliation verifies receipts strictly and leaves unverified manual rows unresolved. Register facts remain history, not ledger postings. No accrual/cash/year-close activation or role/admission relaxation.

Complete default193-file inventory onPython3.11.18/PG16.2 plus legacySQLite:2605nodes=2558pass47existing-skips, thirteen disjoint runs, not one process. The realHTTPwrapper includes four child cases not added twice. A separately enabled existing synthetic transition concurrency case passed after stale test-only expectations were aligned to published1709 exactbusy503 and explicit post-release retry. Full details and residuals: [MANUAL_OPENING_APPLICATION_1708.md](MANUAL_OPENING_APPLICATION_1708.md). main/held0066/journal/native/AI candidates unchanged. NoWindows/hotel-device/whole-financial acceptance.

## Hosted3fdd Linux disposition,25November20:48

Hosted Linux migration succeeded but first full-suite shard had one UID populated restore failure: unsupported transaction_timeout emitted by restore client,2544pass46skip1fail,cycle shard not reached. Workflow-only correction selects/verifiesPG16clients againstPG16service; local actual UID dump/restore/CI qualification passed, not hosted rerun success. Windows not declaredgreen. See HOSTED_PG_CLIENT_1744.md. Noopeningproductrollback or gateweakening.

## AI configuration/routing1747,25November20:52

Installed.env and process settings now resolve model/baseURL consistently, with alias precedence/BOM handling/warnings and directdotenvdependency. Default3bunchanged/notproductionqualified. Bounded polite/everydayAR/EN/ZHcommands reach unchanged explicitreview/approval gateway; text/history queries remain read-only within qualified corpus. No broadgrammar/modelqualityclaim. Focused598pass4existingSQLitevariantskips with actualPGHTTP/consent and transport/regression. Added monthlyrole/nonfinite tests characterize existing behavior only. Details: AI_CONFIG_ROUTING_1747.md. No fullsuite/Windows/deviceacceptance fromthisdelta.


## 1751 qualification checkpoint

Exact shared runner-pin contract:21passed,0skips,1warning, Python3.11.18, actual0068migratedPG16.2 configured (some contract functions are pure/mocked). No broad suite rerun for runner-only change. Local setup retries before this result failed configuration validation and are not counted as test passes. Hosted success is not yet established. See HOSTED_PG_CLIENT_1751.md for exact runner/package failure and source proof.


## Instinct1756: reviewed return registration

Normal POSIX PostgreSQL HR source-review->pendingattendance->P+A/W/H/S/U factual register path, separate immutable0069receipt and verified exact retry, no leave approval/debit/accrual/payroll/month-close. Existing synthetic harness and restore gates unchanged. Windows isolated parser still unavailable and refuses without mutation.25targetedpass0skip onPython3.11.18/PG16.2; independent focused static review accepted after corrections, actual AR390/EN/ZH pixels inspected. Full boundary, source locks, replay and original owner authority: docs/REVIEWED_RETURN_REGISTER_1756.md. This completes a supported register prerequisite, not monthly accrual or Windows/device acceptance.


Exact integrated shared source, based on8526380 runner pin:166passed,0skipped,1warning,67.34seconds. This is the focused register/source/opening/restore/parser/CI-contract suite, not whole-suite, Windows or hotel acceptance.


## Instinct1759: preserve application warnings through in-process migration

fileConfig now keeps existing application loggers enabled; original warning tests unchanged. Real migrated-PG regression fails old behavior and passes fixed behavior. Two existing source checks explicitly readUTF8. Isolated44pass13warnings, independent focused source review accepted. Actual852Linux firstshard3AIwarningfailures identified, no full hosted green or Windows parser claim. Details:docs/MIGRATION_LOGGING_1759.md.
