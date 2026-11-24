# CI preparation, not hosted acceptance

Prepared by Instinct. Source batch approved by the owner for commit/publication; hosted acceptance is still separate.

Both jobs migrate the smoke database and run the complete test inventory through two fresh loopback-only databases. All test files are assigned exactly once, preserving synthetic_leave_ and synthetic_cycle_ safety guards. No create_all/stamp, test exclusion or guard bypass. Existing opt-in tests remain guarded; this is not a promise that every test runs by default.

Manual environment gates currently present: MANUAL_APPROVAL_CONCURRENCY, MANUAL_APPROVAL_LOCK, MANUAL_ARCHIVE_ROUNDTRIP, MANUAL_ATTENDANCE_CONCURRENCY, MANUAL_ATTENDANCE_HTTP, MANUAL_FULLPATH_ACCEPTANCE, MANUAL_GLOBAL_READINESS_CONCURRENCY, MANUAL_LEAVE_TRANSITION_CONCURRENCY, MANUAL_MONTH_REVIEW_HTTP, MANUAL_RECEIPT_ARCHIVE, MANUAL_RETURN_REGISTER_CYCLE, MANUAL_SHIFT_HTTP, MANUAL_SYNTHETIC_MONTH_CYCLE. These require their own documented isolated fixtures; do not turn them all on against the same database. Windows-only tests also retain platform guards.

Local SQLAlchemy/driver endpoint validation rejects all URL query parameters. Collisions refuse before create/drop, only successfully created DBs are removed. Cleanup failure preserves the primary test failure and warns about surviving owned DBs; next attempt refuses that collision.

Windows source uses its own high-port UTF8 cluster rather than starting the preinstalled service. PowerShell, actual Windows lifecycle and hosted green jobs have not been proved locally. Current official runner inventory was checked at https://github.com/actions/runner-images/blob/main/images/windows/Windows2025-Readme.md, but each run still checks actual binaries. Publication and hosted run remain separate gates.

## Approved storage boundary

The owner approved publishing this CI/documentation batch on24November2026. Pip cache and hosted artifact upload are removed. The package job still builds and checks an archive/checksum on its temporary runner, but does not retain or deliver a downloadable artifact. Existing stored artifacts/caches are not deleted by this change.

Standard ubuntu-latest/windows-latest compute is free for this currently public repository under GitHub's current rules. This is not a guarantee about account-wide billing, historic storage, other workflows or a future repository visibility change. Sources: https://docs.github.com/en/actions/reference/runners/github-hosted-runners and https://docs.github.com/en/billing/concepts/product-billing/github-actions . No billing settings or paid services are changed. Triggers remain all branch pushes, main-targeted pull requests and manual dispatch; docs-only pushes also run tests.
