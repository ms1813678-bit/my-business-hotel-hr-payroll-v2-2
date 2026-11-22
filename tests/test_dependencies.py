from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

from app.dependencies import get_current_admin


def make_db(admin):
    from app.models import AdminSession

    db = MagicMock()
    db.query.return_value.filter.return_value.one_or_none.return_value = admin
    # get_current_admin validates the live server-side session record and
    # reads the post-restore regeneration flag; stub both lookups.
    def _get(model, key):
        if model is AdminSession:
            return SimpleNamespace(
                admin_account_id=getattr(admin, "id", None),
                last_seen_at=None,
            )
        return None  # SystemSetting flags absent

    db.get.side_effect = _get
    return db


def make_request(session):
    return SimpleNamespace(session=session, state=SimpleNamespace())


def test_get_current_admin_returns_active_account():
    admin = SimpleNamespace(
        id=1,
        username="test-admin",
        active=True,
        session_version=0,
        role="system_admin",
        display_name="Test Admin",
    )
    request = make_request({
        "admin_username": " test-admin ",
        "admin_session_version": 0,
        "admin_session_id": "test-session-id",
    })

    assert get_current_admin(request, make_db(admin)) is admin


def test_get_current_admin_rejects_missing_session():
    request = make_request({})

    with pytest.raises(HTTPException) as exc:
        get_current_admin(request, make_db(None))

    assert exc.value.status_code == 401


@pytest.mark.parametrize("admin", [None, SimpleNamespace(username="test-admin", active=False)])
def test_get_current_admin_rejects_invalid_or_inactive_account(admin):
    session = {"admin_username": "test-admin", "other": "test-value"}
    request = make_request(session)

    with pytest.raises(HTTPException) as exc:
        get_current_admin(request, make_db(admin))

    assert exc.value.status_code == 401
    assert session == {}
