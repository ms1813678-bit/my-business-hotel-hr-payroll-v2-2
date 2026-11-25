# sync_uid contract (migration 0067, source-qualified, not deployed to a hotel device)

Scope: departments, employees, leave_types, leave_records. This is a stable identity per row, not a sync protocol, disclosure policy, applier or restore behavior.

- Integer primary keys, foreign keys, audit and the employee code allocator are unchanged. uids are per table, not global.
- Column `sync_uid` VARCHAR(36) NOT NULL, unique (`uq_<table>_sync_uid`), canonical lowercase UUID4 (`ck_<table>_sync_uid_v4`, PostgreSQL).
- Upgrade: add nullable column, backfill each row with Python `uuid.uuid4()`, then NOT NULL, default `sync_uid_new()`, constraints, trigger.
- Insert paths: ORM assigns uuid4 (or validates a supplied value); raw and Core inserts use the DB default; an explicit valid uid is accepted; null, empty, malformed, uppercase, non-v4 and duplicate values are rejected by the DB.
- Immutability: BEFORE UPDATE trigger rejects any change or clearing of a non-null uid, including raw and bulk UPDATE. Same-value writes and other column edits are allowed.
- Downgrade: refuses (RuntimeError, nothing changed) if any of the four tables has rows. Fresh installs seed 6 leave_types, so a normal install cannot be downgraded without emptying them. A clean empty schema downgrades and re-upgrades.
- Not covered here: restore epoch or restore admission, field-level disclosure, device/command receipts. Backup/restore qualification is the integrating writer's.
- app/sync_uid.py is a mapping helper only (local ids to uids and back); it validates shape, not policy.

## Release and rollback procedure

Before applying 0067 to any non-disposable installation, stop application writes, take a verified full pre-migration pg_dump archive and prove that archive restores into a separate disposable database. Retain that pre-migration archive securely until acceptance. Do not run the test-only discard helper on an installation. A normal fresh installation already has seeded leave types, so ordinary downgrade refuses. Rollback means restoring the verified pre-migration archive, not deleting rows or resetting Alembic stamps. This source publication does not apply the migration to a hotel device.

Adding/backfilling columns and adding constraints/triggers requires PostgreSQL DDL locks on the four tables and can block writers. Use a planned maintenance window with no concurrent application writes; upgrade inside the existing transactional migration runner. Failed upgrade rolls back its schema/data/version transaction. A post-upgrade rollback to the older schema must use the retained archive, with the application stopped.

The SQL default derives its UUID-shaped value from md5(random(), clock and backend pid); it is not CSPRNG output and must never be used as a secret or authentication token. The UNIQUE constraint rejects collisions. Python ORM/backfill values use uuid.uuid4().

## Qualification and limits, 25 November 2026

Python3.11.18/PostgreSQL16.2 current-source combined targeted run: 417 passed, 6 skipped, 100 warnings in106.51s. The six skips are two dedicated committed reconciliation-race/timeout cases plus four SQLite variants of PostgreSQL-only AI concurrency/recovery cases. PostgreSQL AI counterparts executed. Independent exact-source review accepted the bounded foundation without rerunning the suite. This is not a full-project or network-sync acceptance.

Actual populated linked rows in all four tables plus audit were dumped and restored into a separate database. Exact non-UID values, integer links, identifiers, eight UID constraints, four enabled triggers, two functions and four defaults matched. Restored defaults, null/malformed/duplicate refusal and immutable updates were exercised; a guarded HTTP health subprocess passed. This does not qualify the product encrypted-restore epoch/admission protocol.

The session guard checks actual four-table behavior in rolled-back transactions. Rows roll back; sequence values may advance, leaving harmless gaps. The destructive old-migration test helper is imported by tests only and uses a database/schema NAME allowlist, not strong target isolation. A real database with a matching name would pass that check. Run these tests only against explicitly disposable environments. Existing business-history downgrade assertions remain separate from the UID-loss refusal proof.

Lineage is one head 0067 ->0065. The held0066 experiment is not present or activated. If later adopted, its migration must be deliberately rebased onto the then-current head, never added as a sibling branch. Metadata comparison retains three known employees.code uniqueness/index differences, none UID; server defaults are intentionally not autocompared, but directly and behaviorally verified.

Final shared-tree confirmation:417passed6skipped100warnings108.45s, same scoped selection.
