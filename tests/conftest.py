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
