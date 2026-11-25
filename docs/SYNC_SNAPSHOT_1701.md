# Internal UID snapshot primitive, 25 November 2026

Baseline6fa15a7. This is a read-only internal technical primitive over the existing four0067 UID-enabled entities, not a device export, role projection, revision journal or synchronization implementation.

`read_uid_snapshot(session, entity, uid, fields)` selects only the caller's explicit nonempty distinct scalar columns for one exact canonical UID. Integer identities and the UID column cannot be requested as data fields; identity is returned separately. Supported foreign keys are joined to their parent UID in the same SELECT, so integer IDs do not escape into a prospective planner snapshot. Branch references are refused, including NULL, because branches have no UID foundation. Unknown entities, columns, relationships, malformed/wrong-entity/unknown UIDs and unresolved parents refuse rather than widening. No default all-fields set, row enumeration or fallback by integer ID exists.

## Authority and transaction boundary

This helper does not authenticate or grant field access. A future authenticated administrator-local caller must establish disclosure authority before choosing fields. No route, API, file, queue, logging, cache, network serialization or device copy calls it. Placeholder policies from isolated sync_core are not imported or adopted. Decimal/date values remain Python values, not canonical wire bytes.

Pending ORM new/dirty/deleted state refuses before a query, and no autoflush runs. This is not detection of Core/raw writes already executed in the same transaction. The caller owns transaction isolation and should use a database-enforced read-only transaction when strong read-only scope is required. One call joins its requested parent UIDs in one SELECT; multiple calls do not establish a common snapshot under READ COMMITTED. No lock or write is acquired by the helper. The returned dictionary is caller-owned mutable data, not an immutable decision receipt.

## Qualification

Current isolated source:28passed0skipped27warnings13.29s,16 new actual migrated PostgreSQL16.2 cases and12 existing real-model UID tests. Synthetic schema through0067, removed in fixture teardown. Four entity snapshots preserve values/types and parent UID links; four snapshot calls produce exactly four SELECTs and no identity-map load; all four business tables and audit row JSON values unchanged before/after. Invalid field shapes refuse without a query; pending ORM insert/update/delete refuse without flush; wrong-entity, unknown, malformed UID and out-of-scope branch refuse. A second schema with identical employee integer ID does not bind by UID. A database-enforced READ ONLY transaction succeeds without changing that setting. No visual artifact or UI is created.

## Remaining integration

Actual business rows do not have the isolated sync store's authoritative version/history journal. This helper does not mint a revision, infer ancestry or claim live different-field merge/same-field conflict handling. The existing isolated core/store remains separate. Future planner integration needs grounded version/history, transaction-wide apply/receipt/restore-epoch/admission and field disclosure before any real device copies or business writes. No native Windows/hotel acceptance follows from these tests.

Final exact shared-tree selection:28passed0skipped27warnings13.77s. Independent static review confirmed updated source SHA256b6527852e19e0bc716c0f7d26fe569fd7bdf800791008d15ccc07ffb08cf30a1 and unchanged test55a01c8cb4f928de022e70b387f7b48bb9e74ba8b19353e49691e263ac61d049, without running tests. Experimental removal of only pending-ORM guard caused all three pending-state tests to fail in1.79s; exact guard restored. This detects removal in those fixtures, not every mutation source. Disposable schemas verified absent.
