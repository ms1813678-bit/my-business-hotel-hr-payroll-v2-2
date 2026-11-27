"""Multi-instance sync harness (design note docs/SYNC_DESIGN.md section 5).

Test-only package. It starts one main instance and two device instances as
separate processes, each with its own PostgreSQL cluster, database and port, and
drives the scenarios the design note lists. Nothing here is imported by app/.

Importing this package gives the product's settings module a placeholder
database URL when the caller has not set one, because app.config validates its
settings at import time and the harness imports app.models for the real tables.
Every instance process overwrites DATABASE_URL with its own database before it
imports any app module, so the placeholder is never connected to.
"""

import os as _os

_os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://postgres@127.0.0.1:5432/sync_harness")
_os.environ.setdefault("SESSION_SECRET", "sync-harness-only-session-secret-0123456789")
_os.environ.setdefault("APP_ENV", "test")
