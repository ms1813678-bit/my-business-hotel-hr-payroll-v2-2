# Multi-instance sync harness

One main process and two device processes (roles: assistant, finance), each with
its own PostgreSQL cluster (pgserver, or external PG). Test data only.

    python -m tests.sync_harness.run [--scenario NAME ...] [--json-out F] [--keep --work-dir D]

20 scenarios cover docs/SYNC_DESIGN.md section 5: two-way edits, same-field
conflict accept/reject, delete vs edit, offline reconnect, interrupted sync at
each step (process killed via fault hook), duplicate code, hidden-field and role
visibility, restore refusal, replay, clock skew, atomic main rollback, and a
seeded random run checking no lost change and equal databases.

Engine: uses app.sync_session when it exists, otherwise the reference engine in
this folder (a test double of the design interface). Passing against the double
proves the harness, not the product.
