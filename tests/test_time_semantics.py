"""PostgreSQL TIMESTAMPTZ semantics: aware writers and Cairo date boundaries.

These tests verify behavior against the real database, not just unit-green:
- column defaults write aware UTC values that read back as aware datetimes;
- boundary comparisons give identical results under different session timezones;
- Cairo day boundaries are honored in both winter (UTC+2) and summer (UTC+3).
"""
from datetime import datetime, timezone

from sqlalchemy import text

from app.db import engine, SessionLocal
from app.models import CAIRO_TZ, AuditLog


def test_column_defaults_write_aware_utc():
    """Probe insert is rolled back: migration 0009 forbids DELETE on audit_logs."""
    before = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add(AuditLog(actor="tz-test", action="tz_probe", entity_type="probe", entity_id="1"))
        db.flush()
        row = db.query(AuditLog).filter_by(actor="tz-test").order_by(AuditLog.id.desc()).first()
        assert row is not None
        db.refresh(row)
        assert row.created_at.tzinfo is not None, "read-back must be timezone-aware"
        delta = abs((row.created_at.astimezone(timezone.utc) - before).total_seconds())
        assert delta < 60, "stored instant must match real UTC now, not a shifted wall clock"
        db.rollback()


def _probe_query(session_tz: str) -> tuple[int, int]:
    """Same stored rows, same comparisons, under one session timezone."""
    with engine.connect() as conn:
        assert session_tz in ("UTC", "Africa/Cairo")
        conn.execute(text(f"SET TIME ZONE '{session_tz}'"))
        conn.execute(text("DROP TABLE IF EXISTS tz_probe"))
        conn.execute(text("CREATE TABLE tz_probe (id serial, at timestamptz)"))
        # Known absolute instants: Jan 15 2026 00:30 Cairo winter = 2026-01-14 22:30 UTC;
        # Jul 15 2026 00:30 Cairo summer = 2026-07-14 21:30 UTC.
        conn.execute(text("INSERT INTO tz_probe (at) VALUES (:v)"), {"v": datetime(2026, 1, 14, 22, 30, tzinfo=timezone.utc)})
        conn.execute(text("INSERT INTO tz_probe (at) VALUES (:v)"), {"v": datetime(2026, 7, 14, 21, 30, tzinfo=timezone.utc)})
        aware_jan = conn.execute(
            text("SELECT count(*) FROM tz_probe WHERE at >= :start AND at < :end"),
            {"start": datetime(2026, 1, 15, 0, 0, tzinfo=CAIRO_TZ), "end": datetime(2026, 1, 16, 0, 0, tzinfo=CAIRO_TZ)},
        ).scalar_one()
        aware_jul = conn.execute(
            text("SELECT count(*) FROM tz_probe WHERE at >= :start AND at < :end"),
            {"start": datetime(2026, 7, 15, 0, 0, tzinfo=CAIRO_TZ), "end": datetime(2026, 7, 16, 0, 0, tzinfo=CAIRO_TZ)},
        ).scalar_one()
        conn.execute(text("DROP TABLE tz_probe"))
        conn.commit()
    return aware_jan, aware_jul


def test_cairo_boundary_comparisons_are_session_independent():
    utc_results = _probe_query("UTC")
    cairo_results = _probe_query("Africa/Cairo")
    assert utc_results == cairo_results == (1, 1), (
        "aware Cairo-boundary comparisons must not depend on the session timezone"
    )


def test_cairo_offsets_winter_and_summer():
    assert datetime(2026, 1, 15, tzinfo=CAIRO_TZ).utcoffset().total_seconds() == 2 * 3600
    assert datetime(2026, 7, 15, tzinfo=CAIRO_TZ).utcoffset().total_seconds() == 3 * 3600
