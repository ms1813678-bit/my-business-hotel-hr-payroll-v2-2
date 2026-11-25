# Schema drift: migration chain vs models (November 2026 review)

`alembic check` on a database migrated to head reported differences between
the historical migration chain and `app/models.py`. All items were benign; no
data rewrite was needed.

| Drift | Direction | Resolution |
| --- | --- | --- |
| ix_advance_requests_payroll_run_id | DB had it, model did not | Aligned: index added to the model (no DB change needed) |
| ix_ai_command_approvals_status_expires_at | DB had it, model did not | Aligned: index added to the model |
| ix_ai_conversation_messages_actor_id | DB had it, model did not | Aligned: index added to the model |
| ix_staff_actions_payroll_run_id | DB had it, model did not | Aligned: index added to the model |
| report_presets.created_at nullable | DB: nullable; model: NOT NULL | Aligned: migration 0029 SET NOT NULL; refuses and reports instead of inventing a timestamp if any hand-edited NULL exists |
| employees.code uniqueness | DB: separate UNIQUE constraint + non-unique index; model: one unique index | Left as-is: functionally identical enforcement; aligning it would churn constraints for zero benefit |

The migration history remains the install source of truth; no historical
migration was edited. The model-side index additions only affect future
`create_all` builds (e.g. fresh test databases) so they match what the
migration chain already creates.

## sync_uid (migration 0067, candidate)

| Item | Drift | Decision |
|---|---|---|
| departments/employees/leave_types/leave_records.sync_uid default | DB: `server_default sync_uid_new()` (pure SQL, no extension); model: none (ORM assigns uuid4 in a before_insert listener) | Intentional: raw and Core inserts get a value from the DB, ORM inserts from Python. Alembic's default autogenerate does not compare server defaults. |
| Constraint names | DB and model both use `uq_<table>_sync_uid` and `ck_<table>_sync_uid_v4` | The CHECK (PostgreSQL regex) is `ddl_if(postgresql)` so SQLite `create_all` fixtures still build; the trigger and CHECK exist only on migrated PostgreSQL. |
| Immutability trigger | DB only (`trg_<table>_sync_uid_immutable`) | By design; checked by tests/conftest.py on migrated PostgreSQL. |
