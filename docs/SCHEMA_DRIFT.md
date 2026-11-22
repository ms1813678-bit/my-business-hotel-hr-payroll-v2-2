# Schema drift: migration chain vs models (November 2026 review)

`alembic check` on a database migrated to head (0028) reports differences
between the historical migration chain and `app/models.py`. All items are
benign; no data rewrite or corrective migration was applied.

| Drift | Direction | Consequence | Options |
| --- | --- | --- | --- |
| ix_advance_requests_payroll_run_id | DB has it, model does not | Extra index; lookups still work, tiny write overhead | Keep (harmless) or drop in a future alignment migration |
| ix_ai_command_approvals_status_expires_at | DB has it, model does not | Same as above | Same |
| ix_ai_conversation_messages_actor_id | DB has it, model does not | Same as above | Same |
| employees.code uniqueness | DB: separate UNIQUE constraint + non-unique index; model: one unique index | Functionally identical enforcement | Cosmetic; an alignment migration could swap to the model style, no benefit |
| report_presets.created_at nullable | DB: nullable; model: NOT NULL | DB tolerates NULL the model never writes (server default fills it) | A future migration can backfill-check then SET NOT NULL |
| ix_staff_actions_payroll_run_id | DB has it, model does not | Same as first item | Same |

Decision recorded: leave as-is. The migration history is the install source of
truth; models drift is index cosmetics plus one nullable flag with no write
path producing NULL. Revisit only if a future migration touches these tables.
