"""5t: REAL backup_workflow security capture/finalize on the migrated PostgreSQL test DB (base 778196c).
Port of the stub/SQLite prep tests. Nothing is stubbed: real app.db.SessionLocal, restore_lock, backup_crypto
(real scrypt passphrase), recovery_login_proof. NOT exercised: pg_dump/pg_restore, the schema swap itself,
rollback of restore_backup, the HTTP route, Windows. The archive swap is simulated by rewriting the security rows.
Safety: no drop_all/create_all. The fixture refuses a non-local DB or non-empty target tables, and after each test
deletes everything in those tables (recovery_codes, admin_sessions, admin_accounts, system_settings, departments,
backup_runs) and only the audit_logs rows added during the test (ids above the highest pre-existing id)."""
import json
from datetime import datetime, timezone
import pytest
from sqlalchemy import delete, false, select, text
from sqlalchemy.engine import make_url
from app import backup_workflow as BW
from app import models as M
from app.backup_crypto import SETTING_KEY as BACKUP_KEY, export_public_record, set_passphrase, unwrap_data_key_from_record
from app.db import SessionLocal, engine
from app.recovery_login_proof import SETTING_KEY as PROOF_KEY

pytestmark = pytest.mark.skipif(engine.dialect.name != "postgresql", reason="UNRAN: PostgreSQL required")
NOW = datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc)
TABLES = ("recovery_codes", "admin_sessions", "admin_accounts", "system_settings", "departments",
          "audit_logs", "backup_runs")
PASS = "synthetic-passphrase-123"


@pytest.fixture(autouse=True)
def clean_security_tables():
    url = make_url(str(engine.url))
    assert url.host in ("127.0.0.1", "localhost", None), "refusing non-local database"
    # audit_logs may already hold rows written by other tests in the same CI session. Only the identity and
    # backup tables must start empty; audit_logs is checked by recording the highest existing id and deleting
    # only rows above it afterwards (rows that existed before are never deleted).
    with SessionLocal() as db:
        audit_floor = db.execute(text("SELECT COALESCE(MAX(id), 0) FROM audit_logs")).scalar()
    must_be_empty = tuple(t for t in TABLES if t != "audit_logs")

    def purge():
        with SessionLocal.begin() as db:
            db.execute(text("SET LOCAL session_replication_role = replica"))  # bypass sole-admin delete trigger
            for t in must_be_empty:
                db.execute(text(f"DELETE FROM {t}"))
            db.execute(text("DELETE FROM audit_logs WHERE id > :floor"), {"floor": audit_floor})
    with SessionLocal() as db:
        busy = {t: db.execute(text(f"SELECT count(*) FROM {t}")).scalar() for t in must_be_empty}
    assert not any(busy.values()), f"refusing to run: target tables not empty {busy}"
    try:
        yield
    finally:
        purge()


def acct(db, name, role, h, active=True, must=False, pend=False):
    a = M.AdminAccount(username=name, password_hash=h, role=role, active=active, must_change_password=must,
                       pending_credential_reset=pend, display_name=name.title())
    db.add(a); db.flush(); return a


def code(db, a, h, used=None):
    db.add(M.RecoveryCode(admin_account_id=a.id, code_hash=h, code_lookup=h[:8], used_at=used))


def run_finalize(snapshot, tmp, material=None):
    arch = tmp / "pre.zip"; arch.write_bytes(b"x")
    BW._finalize_restored_security(selected=tmp / "sel.zip", security_snapshot=snapshot, actor="admin", reason="test",
                                   manifest_created_at="2026-11-20", pre_restore_path=arch,
                                   pre_restore_hash="a" * 64, completed_at=NOW, recovery_login_material=material)


def accounts():
    with SessionLocal() as db:
        return {a.username: a for a in db.scalars(select(M.AdminAccount)).all()}


def wipe_security():
    with SessionLocal.begin() as db:
        db.execute(text("SET LOCAL session_replication_role = replica"))  # a schema swap is not a row delete
        for t in ("recovery_codes", "admin_sessions", "admin_accounts", "system_settings"):
            db.execute(text(f"DELETE FROM {t}"))


def build_live_case(ghost_in_live=False):
    with SessionLocal.begin() as db:
        adm = acct(db, "admin", "system_admin", "H_NEW")
        acct(db, "fin", "finance", "H_FIN_NEW", active=False)
        acct(db, "assist", "assistant_manager", "H_AS_NEW")
        if ghost_in_live:
            acct(db, "ghost", "finance", "H_GHOST_LIVE")
        code(db, adm, "CODE_NEW_USED_" + "x" * 8, used=NOW); code(db, adm, "CODE_NEW_LIVE_" + "y" * 8)
        db.add(M.SystemSetting(key="login_security", value='{"fails":3}', updated_by="sys"))
        db.add(M.SystemSetting(key="recovery_lookup_pepper", value="PEPPER_NEW", updated_by="sys"))
    with SessionLocal() as db:
        snap = BW._capture_security_state(db)
    wipe_security()   # archive swap simulated: archive-era security rows
    with SessionLocal.begin() as db:
        adm = acct(db, "admin", "system_admin", "H_OLD")
        acct(db, "fin", "finance", "H_FIN_OLD", active=True)
        d = acct(db, "ghost", "finance", "H_GHOST", active=True)
        code(db, adm, "CODE_OLD_REVIVED_" + "z" * 8); code(db, d, "GHOST_CODE_" + "g" * 8)
        db.add(M.SystemSetting(key="recovery_lookup_pepper", value="PEPPER_OLD", updated_by="sys"))
        db.add(M.SystemSetting(key="telegram_alerts", value=json.dumps(
            {"chat_id": "1", "bind_nonce": "N1", "bind_nonce_expires_at": "t"}), updated_by="sys"))
        db.add(M.AdminSession(id="s" * 32, admin_account_id=adm.id))
        if db.scalar(select(M.Department).where(M.Department.name == "Front Office")) is None:
            db.add(M.Department(name="Front Office"))   # business row: survives the simulated swap
    return snap


def test_live_restore_carries_credentials_and_locks_archive_only(tmp_path):
    run_finalize(build_live_case(), tmp_path)
    a = accounts()
    assert a["admin"].password_hash == "H_NEW" and a["admin"].role == "system_admin" and a["admin"].active
    assert a["fin"].password_hash == "H_FIN_NEW" and a["fin"].active is False
    assert a["assist"].password_hash == "H_AS_NEW" and a["assist"].role == "assistant_manager"
    g = a["ghost"]
    assert (g.active, g.must_change_password, g.pending_credential_reset) == (False, True, True)
    assert g.password_hash == "H_GHOST"   # old hash stays on the row; lock + reset flag is the protection
    with SessionLocal() as db:
        codes = db.scalars(select(M.RecoveryCode)).all()
        assert not any(c.code_hash.startswith(("CODE_OLD", "GHOST")) for c in codes)
        assert any(c.code_hash.startswith("CODE_NEW_USED_") and c.used_at for c in codes)
        assert db.get(M.SystemSetting, "recovery_lookup_pepper").value == "PEPPER_NEW"
        assert db.get(M.SystemSetting, "login_security").value == '{"fails":3}'
        assert db.scalars(select(M.AdminSession)).all() == []
        assert db.get(M.SystemSetting, "restore_epoch").value
        tg = json.loads(db.get(M.SystemSetting, "telegram_alerts").value)
        assert tg["bind_nonce"] == "" and tg["bind_nonce_used"] is True and tg["chat_id"] == "1"
        logs = db.scalars(select(M.AuditLog)).all()
        acts = [r.action for r in logs]
        for want in ("restore_resurrected_accounts_locked", "restore_credentials_carried_over", "backup_restored"):
            assert want in acts
        assert next(r for r in logs if r.action == "restore_resurrected_accounts_locked").new_value == "ghost"
        assert db.scalars(select(M.Department)).one().name == "Front Office"
        assert "post_restore_regen_required" not in {s.key for s in db.scalars(select(M.SystemSetting))}
        assert db.get(M.SystemSetting, PROOF_KEY) is None or not db.get(M.SystemSetting, PROOF_KEY).value


def test_live_restore_epoch_changes_each_time(tmp_path):
    run_finalize(build_live_case(), tmp_path)
    with SessionLocal() as db: e1 = db.get(M.SystemSetting, "restore_epoch").value
    wipe_security()
    run_finalize(build_live_case(), tmp_path)
    with SessionLocal() as db: e2 = db.get(M.SystemSetting, "restore_epoch").value
    assert e1 != e2


@pytest.mark.parametrize("passphrase", [True, False])
def test_disaster_path_forces_password_change_and_gates(tmp_path, passphrase):
    material = None
    with SessionLocal.begin() as db:
        acct(db, "admin", "system_admin", "H_ARCH"); acct(db, "fin", "finance", "H_F", active=False)
        db.add(M.SystemSetting(key="telegram_alerts", value=json.dumps({"bind_nonce": "N"}), updated_by="s"))
        if passphrase:
            set_passphrase(db, PASS, "test")
            rec = export_public_record(db)
            material = (rec, unwrap_data_key_from_record(rec, PASS), "b" * 64)
    run_finalize({"accounts": {}, "codes": [], "settings": {}}, tmp_path, material)
    a = accounts()
    assert all(x.must_change_password for x in a.values())
    assert a["admin"].active and a["fin"].active is False and a["admin"].role == "system_admin"
    with SessionLocal() as db:
        keys = {s.key for s in db.scalars(select(M.SystemSetting))}
        assert "post_restore_regen_required" in keys
        assert ("post_restore_passphrase_required" in keys) is passphrase
        assert json.loads(db.get(M.SystemSetting, "telegram_alerts").value)["bind_nonce"] == ""
        proof = db.get(M.SystemSetting, PROOF_KEY)
        if passphrase:
            p = json.loads(proof.value)
            assert p["archive_sha256"] == "b" * 64 and p["restore_epoch"] == db.get(M.SystemSetting, "restore_epoch").value
        else:
            assert proof is None


def test_pre_restore_history_row_not_duplicated(tmp_path):
    snap = build_live_case()
    run_finalize(snap, tmp_path)
    with SessionLocal() as db: n1 = len(db.scalars(select(M.BackupRun)).all())
    assert n1 == 1
    run_finalize(snap, tmp_path)
    with SessionLocal() as db: assert len(db.scalars(select(M.BackupRun)).all()) == n1


def test_capture_snapshot_shape_and_secret_keys():
    with SessionLocal.begin() as db:
        a = acct(db, "admin", "system_admin", "H"); code(db, a, "C" * 12)
        db.add(M.SystemSetting(key="reauth_security", value="r", updated_by="s"))
        db.add(M.SystemSetting(key="unrelated", value="u", updated_by="s"))
    with SessionLocal() as db:
        s = BW._capture_security_state(db)
    assert set(s) == {"accounts", "codes", "settings"} and s["accounts"]["admin"]["role"] == "system_admin"
    assert s["settings"] == {"reauth_security": "r"}
    assert BW._SECURITY_SETTING_KEYS == ("login_security", "recovery_security", "reauth_security",
                                         "recovery_lookup_pepper", "telegram_alerts")


# ---- negative controls: the main assertions must be able to fail ----
def test_control_account_known_to_live_state_is_not_locked(tmp_path):
    run_finalize(build_live_case(ghost_in_live=True), tmp_path)
    g = accounts()["ghost"]
    assert g.active is True and g.pending_credential_reset is False and g.password_hash == "H_GHOST_LIVE"


def test_control_without_session_delete_sessions_survive(tmp_path, monkeypatch):
    real = BW.delete
    monkeypatch.setattr(BW, "delete", lambda t: real(t).where(false()) if t is M.AdminSession else real(t))
    run_finalize(build_live_case(), tmp_path)
    with SessionLocal() as db:
        assert len(db.scalars(select(M.AdminSession)).all()) == 1


def test_per_account_login_counters_carry_over_and_archive_ones_do_not_revive(tmp_path):
    with SessionLocal.begin() as db:
        acct(db, "admin", "system_admin", "H_NEW")
        db.add(M.SystemSetting(key="login_security:live", value='{"failed_attempts": 2, "locked_until": null}', updated_by="sys"))
    with SessionLocal() as db:
        snap = BW._capture_security_state(db)
    assert snap["settings"]["login_security:live"].startswith('{"failed_attempts": 2')
    wipe_security()
    with SessionLocal.begin() as db:
        acct(db, "admin", "system_admin", "H_OLD")
        db.add(M.SystemSetting(key="login_security:archive", value='{"failed_attempts": 3, "locked_until": "2999-01-01T00:00:00+00:00"}', updated_by="sys"))
    run_finalize(snap, tmp_path)
    with SessionLocal() as db:
        assert db.get(M.SystemSetting, "login_security:live").value.startswith('{"failed_attempts": 2')
        assert db.get(M.SystemSetting, "login_security:archive") is None
