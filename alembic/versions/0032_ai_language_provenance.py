"""Restart AI conversation language provenance on honest footing.

Revision ID: 0032_ai_language_provenance
Revises: 0031_ai_conversation_language
Create Date: 2026-11-22

The first version of 0031 defaulted the new language column to "ar", which
fabricated origin metadata for entries whose language was never recorded
(including English/Chinese Q&A written before the column existed). Any
database that already applied that version carries those false stamps.

This migration makes the repair reproducible for every database, fresh or
already migrated: every row that exists at upgrade time predates grounded
language stamping, so its provenance is unproven and becomes "unknown";
the column default is corrected to "unknown" as well. Message texts are
never touched. Entries written by the application after this upgrade carry
their true, grounded language.
"""
from alembic import op
import sqlalchemy as sa


revision = "0032_ai_language_provenance"
down_revision = "0031_ai_conversation_language"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("ai_conversation_messages")
    }
    if "language" in columns:
        op.alter_column(
            "ai_conversation_messages",
            "language",
            server_default="unknown",
        )
        op.execute(
            "UPDATE ai_conversation_messages SET language='unknown' "
            "WHERE language <> 'unknown'"
        )


def downgrade():
    # Provenance once marked unknown must never be re-fabricated.
    pass
