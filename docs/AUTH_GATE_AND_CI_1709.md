# Auth gate and CI regression repair, Instinct, 25 November 2026

## Actual current HTTP gate defect

The existing request middleware holds a shared restore advisory lock on a dedicated connection. Authentication then acquired a second blocking shared lock on its own database session. If an exclusive restore waiter arrived between those steps, authentication queued behind it while the restore waited for the request's first lock. This is an existing path, not a journal-only defect.

The shared helper now uses PostgreSQL's try-shared-lock and returns HTTP 503 with Retry-After: 5 when acquisition is unavailable. It never proceeds to protected reads without acquiring the lock. No middleware exemption, identity/role/session/epoch check, credential-write lock, or restore transaction is removed or bypassed. Other callers of the shared helper now receive the same transient refusal rather than waiting when the lock is unavailable. Existing middleware admission waiting and bounds are unchanged.

A real TestClient request logs in through the actual route/CSRF/session, passes the actual middleware, pauses before the real authentication helper, queues an exclusive PostgreSQL waiter, verifies its Lock/advisory state, then resumes authentication. It returns 503, releases the middleware lock, lets the exclusive waiter acquire, and succeeds with the same signed cookie after release. The counterfactual old blocking helper fails this test as HTTP 500 under a test-only 500ms safety timeout. This cap is not part of the product. Separate real-PG tests cover same-session reentrant shared locks and refusal while an exclusive holder is active.

This exercises the actual middleware/auth request geometry, not an actual encrypted restore/schema swap inside that HTTP race. Existing focused restore epoch/reconciliation/recovery tests remain. The isolated department journal1704 is still unadopted; no revision schema, baseline hook or journal route is integrated by this change. Plain dump/restore geometry proof1705 remains distinct from product workflow acceptance.

## Confirmed hosted test regressions

Read the actual raw Linux suite log at published b6134e4:
https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2/actions/runs/45662256870/job/139374908128

It recorded 10 failed,2369 passed,46 skipped. Seven failures were real ordinary Arabic late-attendance questions incorrectly classified as an employee-name selector. Two assertions expected the superseded, overconfident no-employee wording. One roundtrip test imported the developer-only pgserver package unconditionally.

- Count prefixes كام/كم now suppress a name filter only for bare indefinite موظف/حارس. Explicit employee codes retain priority; definite الموظف/الحارس name selectors never broaden silently. Existing كل/أي/اي/جميع behavior is unchanged. Direct and actual structured-answer regressions cover the definite-name cases. Current honest no-match wording is asserted positively and negatively.
- The UID roundtrip test uses the existing product PostgreSQL client finder, not pgserver. It still creates real disposable source/target databases, dumps/restores all four entities and audit/history, checks identities/constraints/triggers/defaults and runs the behavioral guard. No new skip or dependency was added. Matching pg_dump/pg_restore client availability/version remains a runtime prerequisite; developer run used PostgreSQL16.2 clients on PATH.

The newer Windows job at2dee7cd failed immediately launching its migration command, with exit -1073741502 and no Python traceback; cleanup steps also failed. This is not evidence of a migration-code root cause, and this patch does not claim to fix that separate process-start failure:
https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2/actions/runs/45665746608/job/139387288516

## Qualification

Exact combined shared tree:222 passed,zero skipped,29 warnings,60.58seconds, Python3.11.18. Configured database PostgreSQL16.2 with actual0067 migrations; existing unit fixtures include SQLite. Not222 PostgreSQL-only behavioral cases and not a full-suite or Windows acceptance. Separate old-selector counterfactual:9failed81passed; old-blocking-helper counterfactual:1failed. Independent review is static source review, not independent execution. Final disposition is recorded in the publication checkpoint. No layout, native or hotel-device acceptance claim. Hotel trial remains strictly last, outside development percentage accounting.

The helper message is neutral and translated AR/EN/ZH: other credential writers share the advisory key, so contention alone does not prove a restore is active. Immediate503 is intentional, no polling added. The actual HTTP race runs in all three languages.

Final supported Python3.11.18 shared verification after neutral AR/EN/ZH message:222passed0skipped29warnings60.58seconds. Static source acceptance received; no independent test rerun.
