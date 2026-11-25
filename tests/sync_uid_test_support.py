"""Test-only helper for suites that exercise OLDER migrations' downgrades.

0067 deliberately refuses to downgrade when any of its four tables has rows.
Older-migration tests keep their rows and still need to walk below 0067, so they
discard the UID objects in the disposable test schema first, exactly as the
0067 downgrade would, and move the version row back to 0065. The refusal itself
is proven in tests/test_sync_uid_migration.py; this never runs outside tests.
"""
from sqlalchemy import text

REVISION = '0067_sync_uid_base_entities'
PARENT = '0065_overtime_day_review'
TABLES = ('departments', 'employees', 'leave_types', 'leave_records')


def discard_sync_uid_for_older_downgrade(connection):
    database, schema = connection.execute(text('SELECT current_database(),current_schema()')).one()
    disposable_database = database.startswith(('test_', 'synthetic_', 'checklist', 'guard_', 'uid_')) or database.endswith('_test')
    isolated_schema = schema.startswith(('migration_test_', 'leave_precision_', 'synthetic_ot_migration_'))
    if not (disposable_database or isolated_schema):
        raise RuntimeError('UID test-only discard requires an explicit disposable database or migration schema')
    current = connection.execute(text('SELECT version_num FROM alembic_version')).scalar()
    if current == '0068_manual_opening_identity':
        from tests.test_leave_precision_migration import migrate
        migrate(connection, REVISION, down=True)
        current = connection.execute(text('SELECT version_num FROM alembic_version')).scalar()
    if current != REVISION:
        return False
    for table in TABLES:
        connection.execute(text(f'DROP TRIGGER trg_{table}_sync_uid_immutable ON {table}'))
        connection.execute(text(f'ALTER TABLE {table} DROP COLUMN sync_uid'))
    connection.execute(text('DROP FUNCTION sync_uid_immutable()'))
    connection.execute(text('DROP FUNCTION sync_uid_new()'))
    connection.execute(text('UPDATE alembic_version SET version_num=:p'), {'p': PARENT})
    return True
