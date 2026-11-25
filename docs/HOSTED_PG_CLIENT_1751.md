# Hosted runner correction, 25 November 2026

The authenticated log for 563329e Linux job 139435730677 confirms a failure before migration or tests. The latest runner was Ubuntu 26.04 Resolute. Version-16 clients were absent, apt update succeeded, and apt could not locate postgresql-client-16 (exit 100). The 1744 fallback incorrectly assumed the latest runner repository carried that version.

The Linux job now pins ubuntu-24.04. Its current official image inventory lists PostgreSQL 16.16; Ubuntu Noble publishes postgresql-client-16. Existing explicit absolute-path selection, fail-fast version checks, separate effective-PATH verification, PostgreSQL 16 service, migrations and full suite are unchanged. Windows is unchanged. No new external package repository, server-major change, SQL filtering, skip or error suppression.

Sources read 25 November 2026:
- https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md
- https://packages.ubuntu.com/noble/postgresql-client-16
- https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2/actions/runs/45679438529/job/139435730677

Local qualification covers the workflow contract and suite runner tests, not an actual hosted image or apt install. A new hosted run is required to confirm migration and suite execution. Neither the old Windows failure nor the product backup-client finder is fixed by this runner pin.


## 1751 qualification checkpoint

Exact shared runner-pin contract:21passed,0skips,1warning, Python3.11.18, actual0068migratedPG16.2 configured (some contract functions are pure/mocked). No broad suite rerun for runner-only change. Local setup retries before this result failed configuration validation and are not counted as test passes. Hosted success is not yet established. See HOSTED_PG_CLIENT_1751.md for exact runner/package failure and source proof.
