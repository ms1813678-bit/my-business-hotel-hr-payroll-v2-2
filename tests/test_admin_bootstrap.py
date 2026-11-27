import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.db import Base
from app.models import AdminAccount
from app.security import verify_password
from scripts.create_admin import create_admin_account


@pytest.fixture()
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


def test_bootstrap_creates_one_hashed_admin(db):
    admin = create_admin_account(db, " first-admin ", "Valid-Test-Password-123!")
    db.commit()
    assert admin.username == "first-admin"
    assert admin.singleton_key == 1 and admin.active
    assert verify_password("Valid-Test-Password-123!", admin.password_hash)


def test_bootstrap_refuses_second_admin(db):
    create_admin_account(db, "first-admin", "Valid-Test-Password-123!")
    db.commit()
    with pytest.raises(ValueError, match="already exists"):
        create_admin_account(db, "second-admin", "Another-Valid-Password-456!")


def test_database_allows_named_accounts_and_rejects_unknown_role(db):
    create_admin_account(db, "first-admin", "Valid-Test-Password-123!")
    db.commit()
    db.add(AdminAccount(
        username="finance-user", password_hash="fake", active=True,
        role="finance", display_name="Finance Person",
    ))
    db.commit()
    db.add(AdminAccount(
        username="bogus-user", password_hash="fake", active=True, role="superuser",
    ))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_admin_accounts_are_never_deleted(db):
    create_admin_account(db, "first-admin", "Valid-Test-Password-123!")
    db.commit()
    admin = db.scalar(select(AdminAccount))
    # Deactivation is the supported retirement path; the sole active system
    # administrator is protected at the account-management routes.
    admin.active = False
    db.commit()
    admin.active = True
    db.commit()
    db.delete(admin)
    with pytest.raises(ValueError, match="cannot be deleted"):
        db.flush()


def test_setup_rerun_skips_the_admin_step_when_an_admin_exists(db, monkeypatch, capsys):
    import scripts.create_admin as module

    create_admin_account(db, "first-admin", "Valid-Test-Password-123!")
    db.commit()
    monkeypatch.setattr(module, "SessionLocal", lambda: db)
    monkeypatch.setattr(module.sys, "argv", ["create_admin", "--skip-if-exists"])
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(AssertionError("must not prompt")))
    monkeypatch.setattr(module, "getpass", lambda *a: (_ for _ in ()).throw(AssertionError("must not prompt")))
    module.main()
    assert "already exists" in capsys.readouterr().out
    assert db.scalar(select(AdminAccount.username)) == "first-admin"
