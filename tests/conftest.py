"""Session guard: the suite only runs against a fully migrated database.

The application relies on objects that ``Base.metadata.create_all`` never
creates (for example the audit-log immutability trigger from migration 0009).
Running the suite against a ``create_all``-built database can therefore pass
while the real install would behave differently. Require the configured
PostgreSQL database to sit at the alembic head before any test runs.

Prepare a test database with ``python -m scripts.initialize_database`` (or
``alembic upgrade head``) and point ``DATABASE_URL`` at it. See
docs/TESTING.md.
"""

from pathlib import Path

import pytest
from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _alembic_head() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


@pytest.fixture(autouse=True, scope="session")
def require_migrated_database():
    from app.db import engine

    if engine.dialect.name != "postgresql":
        return
    expected = _alembic_head()
    with engine.connect() as connection:
        try:
            current = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        except Exception:
            current = None
    if current != expected:
        pytest.fail(
            "The test database is not migrated to the alembic head "
            f"(found: {current or 'no alembic_version table'}, expected: {expected}). "
            "Tests that run on a create_all-built database miss real-install "
            "behavior such as the audit-log immutability trigger. Initialize it "
            "with `python -m scripts.initialize_database` or `alembic upgrade head` "
            "before running the suite. See docs/TESTING.md.",
            pytrace=False,
        )
    # A matching version stamp is not proof of a real migration: a
    # create_all-built database can be stamped with the head revision while
    # missing everything the migrations create beyond tables. Verify the
    # actual objects the suite depends on.
    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(text(
                "SELECT tgname FROM pg_trigger "
                "WHERE tgrelid = to_regclass('audit_logs') AND tgenabled = 'O'"
            ))
        }
    required = (
        {"trg_audit_logs_immutable"}
        if engine.dialect.name == "postgresql"
        else {"trg_audit_logs_no_update", "trg_audit_logs_no_delete"}
    )
    missing = required - triggers
    if missing:
        pytest.fail(
            "The test database is stamped at the alembic head but is missing "
            f"enabled database objects the real migration chain creates: {sorted(missing)}. "
            "It was likely built with create_all and stamped manually, or a "
            "required trigger was disabled, so tests would miss real-install "
            "behavior. Rebuild it with "
            "`python -m scripts.initialize_database` or `alembic upgrade head`. "
            "See docs/TESTING.md.",
            pytrace=False,
        )
    if engine.dialect.name == "postgresql":
        # An enabled-looking trigger name is still not proof: probe the actual
        # behavior with a rolled-back INSERT+UPDATE so a same-named but
        # ineffective trigger cannot make the suite look protected. The
        # transaction always rolls back, so the probe leaves no data behind.
        probe_error = None
        connection = engine.connect()
        transaction = connection.begin()
        try:
            probe_id = connection.execute(text(
                "INSERT INTO audit_logs (actor, action, entity_type, entity_id, created_at) "
                "VALUES ('suite_guard_probe', 'guard_probe', 'suite', '0', now()) RETURNING id"
            )).scalar()
            connection.execute(
                text("UPDATE audit_logs SET actor = actor WHERE id = :id"), {"id": probe_id}
            )
        except Exception as exc:  # noqa: BLE001 - the trigger's exception type is driver-specific
            probe_error = exc
        finally:
            # Roll back explicitly even when the probe failed: engine.begin()
            # would commit the probe INSERT if the UPDATE wrongly succeeded,
            # and pytest.fail inside the with block would abort the commit but
            # the explicit rollback is the guarantee that no probe row remains.
            try:
                transaction.rollback()
            finally:
                connection.close()
        if probe_error is None:
            pytest.fail(
                "The audit log immutability trigger exists but did not block an "
                "UPDATE probe, so tests would not verify real-install protection. "
                "Rebuild the test database with `python -m scripts.initialize_database` "
                "or `alembic upgrade head`. See docs/TESTING.md.",
                pytrace=False,
            )
        if "immutable" not in str(probe_error):
            raise probe_error
