"""Sync change log: append-only per-device record of field changes (sync core, part 1a).

Only this table is added. No business table, column or row changes. Rows are never updated or deleted,
except that the main device may stamp main_seq once (NULL -> number) when it accepts a change.
"""
from alembic import op
import sqlalchemy as sa

revision = '0084_sync_change_log'
down_revision = '0083_leave_month_correction'
branch_labels = None
depends_on = None

UID_REGEX = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


def upgrade():
    op.create_table('sync_change_log',
        sa.Column('id', sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column('origin_device_id', sa.String(64), nullable=False),
        sa.Column('origin_counter', sa.BigInteger(), nullable=False),
        sa.Column('main_seq', sa.BigInteger(), nullable=True),
        sa.Column('restore_epoch', sa.String(128), nullable=True),
        sa.Column('op', sa.String(10), nullable=False),
        sa.Column('entity', sa.String(40), nullable=False),
        sa.Column('uid', sa.String(36), nullable=False),
        sa.Column('field', sa.String(80), nullable=False),
        sa.Column('old_value', sa.Text(), nullable=True),
        sa.Column('new_value', sa.Text(), nullable=True),
        sa.Column('local_time', sa.String(27), nullable=False),
        sa.Column('prev_hash', sa.String(64), nullable=False),
        sa.Column('row_hash', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.UniqueConstraint('origin_device_id', 'origin_counter', name='uq_sync_change_log_device_counter'),
        sa.UniqueConstraint('main_seq', name='uq_sync_change_log_main_seq'),
        sa.UniqueConstraint('row_hash', name='uq_sync_change_log_row_hash'),
        sa.CheckConstraint('origin_counter >= 1', name='ck_sync_change_log_counter'),
        sa.CheckConstraint('main_seq IS NULL OR main_seq >= 1', name='ck_sync_change_log_main_seq'),
        sa.CheckConstraint("op IN ('set','create','delete')", name='ck_sync_change_log_op'),
        sa.CheckConstraint("uid ~ '" + UID_REGEX + "'", name='ck_sync_change_log_uid_v4'),
        sa.CheckConstraint("(op = 'set') = (field <> '*')", name='ck_sync_change_log_marker_field'))
    op.execute("""CREATE FUNCTION prevent_sync_change_log_mutation() RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'UPDATE' AND OLD.main_seq IS NULL AND NEW.main_seq IS NOT NULL
           AND NEW.id = OLD.id AND NEW.origin_device_id = OLD.origin_device_id
           AND NEW.origin_counter = OLD.origin_counter AND NEW.op = OLD.op AND NEW.entity = OLD.entity
           AND NEW.uid = OLD.uid AND NEW.field = OLD.field AND NEW.local_time = OLD.local_time
           AND NEW.prev_hash = OLD.prev_hash AND NEW.row_hash = OLD.row_hash AND NEW.created_at = OLD.created_at
           AND NEW.old_value IS NOT DISTINCT FROM OLD.old_value AND NEW.new_value IS NOT DISTINCT FROM OLD.new_value
           AND NEW.restore_epoch IS NOT DISTINCT FROM OLD.restore_epoch THEN
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'sync_change_log is append-only';
    END; $$ LANGUAGE plpgsql""")
    op.execute('CREATE TRIGGER trg_sync_change_log_row BEFORE UPDATE OR DELETE ON sync_change_log '
               'FOR EACH ROW EXECUTE FUNCTION prevent_sync_change_log_mutation()')
    op.execute('CREATE TRIGGER trg_sync_change_log_truncate BEFORE TRUNCATE ON sync_change_log '
               'FOR EACH STATEMENT EXECUTE FUNCTION prevent_sync_change_log_mutation()')


def downgrade():
    if op.get_bind().execute(sa.text('SELECT count(*) FROM sync_change_log')).scalar_one():
        raise RuntimeError('Refusing downgrade: sync change log rows exist.')
    op.execute('DROP TRIGGER trg_sync_change_log_truncate ON sync_change_log')
    op.execute('DROP TRIGGER trg_sync_change_log_row ON sync_change_log')
    op.drop_table('sync_change_log')
    op.execute('DROP FUNCTION prevent_sync_change_log_mutation()')
