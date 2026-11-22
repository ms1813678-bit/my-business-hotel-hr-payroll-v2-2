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
