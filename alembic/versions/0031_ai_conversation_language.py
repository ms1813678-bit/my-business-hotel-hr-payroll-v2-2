"""Track the language each AI conversation entry was generated in.

Revision ID: 0031_ai_conversation_language
Revises: 0030_leave_type_name_zh
Create Date: 2026-11-22
"""
from alembic import op
import sqlalchemy as sa


revision = "0031_ai_conversation_language"
down_revision = "0030_leave_type_name_zh"
branch_labels = None
depends_on = None


def upgrade():
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("ai_conversation_messages")
    }
    if "language" not in columns:
        # Rows that predate this column have no recorded origin language.
        # Marking them "unknown" avoids fabricating provenance; the UI shows
        # no language badge for unknown entries. Originals stay untouched.
        op.add_column(
            "ai_conversation_messages",
            sa.Column(
                "language",
                sa.String(length=8),
                nullable=False,
                server_default="unknown",
            ),
        )


def downgrade():
    columns = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("ai_conversation_messages")
    }
    if "language" in columns:
        op.drop_column("ai_conversation_messages", "language")
