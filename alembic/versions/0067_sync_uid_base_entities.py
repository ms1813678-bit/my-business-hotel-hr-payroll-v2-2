"""Add immutable replication identity (sync_uid) to departments, employees, leave_types, leave_records.

Integer primary keys, foreign keys, employee codes, business values and audit history are untouched.
Existing rows get a stdlib uuid4 once. Afterwards the column is NOT NULL, unique (named), CHECKed to
canonical lowercase UUID4, and immutable via trigger, so raw/bulk SQL is covered too. Raw SQL inserts
that omit the column get sync_uid_new() (pure SQL, no extension, no PostgreSQL-version dependency).
Downgrade refuses while any of the four tables has rows, because dropping would lose stable identities
and a later upgrade would mint different ones. No destructive reset is performed or implied.
Lineage: follows the published head 0065; 0066 is a separate held candidate and is not part of this.
"""
import uuid

import sqlalchemy as sa
from alembic import op

revision = "0067_sync_uid_base_entities"
down_revision = "0065_overtime_day_review"
branch_labels = None
depends_on = None

TABLES = ("departments", "employees", "leave_types", "leave_records")
UID_REGEX = "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


def _require_postgresql():
    if op.get_bind().dialect.name != "postgresql":
        raise RuntimeError("sync_uid migration is supported on PostgreSQL only")


def upgrade():
    _require_postgresql()
    bind = op.get_bind()
    op.execute("""
        CREATE FUNCTION sync_uid_new() RETURNS varchar(36) AS $$
            SELECT substr(h, 1, 8) || '-' || substr(h, 9, 4) || '-4' || substr(h, 14, 3) || '-'
                   || substr('89ab', 1 + (get_byte(decode(substr(h, 17, 2), 'hex'), 0) % 4), 1)
                   || substr(h, 18, 3) || '-' || substr(h, 21, 12)
            FROM (SELECT md5(random()::text || clock_timestamp()::text || pg_backend_pid()::text) AS h) s
        $$ LANGUAGE sql VOLATILE
    """)
    op.execute("""
        CREATE FUNCTION sync_uid_immutable() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'sync_uid is immutable';
        END;
        $$ LANGUAGE plpgsql
    """)
    for table in TABLES:
        op.add_column(table, sa.Column("sync_uid", sa.String(36), nullable=True))
        ids = [r[0] for r in bind.execute(sa.text(f"SELECT id FROM {table} ORDER BY id"))]
        if ids:
            bind.execute(sa.text(f"UPDATE {table} SET sync_uid = :u WHERE id = :i"),
                         [{"u": str(uuid.uuid4()), "i": i} for i in ids])
        op.alter_column(table, "sync_uid", existing_type=sa.String(36), nullable=False,
                        server_default=sa.text("sync_uid_new()"))
        op.create_unique_constraint(f"uq_{table}_sync_uid", table, ["sync_uid"])
        op.create_check_constraint(f"ck_{table}_sync_uid_v4", table, f"sync_uid ~ '{UID_REGEX}'")
        op.execute(f"""
            CREATE TRIGGER trg_{table}_sync_uid_immutable
            BEFORE UPDATE ON {table}
            FOR EACH ROW WHEN (OLD.sync_uid IS DISTINCT FROM NEW.sync_uid)
            EXECUTE FUNCTION sync_uid_immutable()
        """)


def downgrade():
    _require_postgresql()
    bind = op.get_bind()
    populated = [t for t in TABLES if bind.execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {t})")).scalar()]
    if populated:
        raise RuntimeError(
            "Refusing to downgrade: removing sync_uid would lose stable identities in "
            + ", ".join(populated) + ". Nothing was changed.")
    for table in TABLES:
        op.execute(f"DROP TRIGGER trg_{table}_sync_uid_immutable ON {table}")
        op.drop_constraint(f"ck_{table}_sync_uid_v4", table, type_="check")
        op.drop_constraint(f"uq_{table}_sync_uid", table, type_="unique")
        op.drop_column(table, "sync_uid")
    op.execute("DROP FUNCTION sync_uid_immutable()")
    op.execute("DROP FUNCTION sync_uid_new()")
