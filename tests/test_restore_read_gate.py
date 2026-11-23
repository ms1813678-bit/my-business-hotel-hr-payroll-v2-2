"""Pin: the restore-read-gate exemption table must match the credential-write
lock call sites in app/main.py. A new gated write site without an exemption
(middleware shared lock + exclusive upgrade = self-deadlock) fails here."""
import re
from pathlib import Path

from app.restore_lock import GATE_EXEMPT_EXACT, GATE_EXEMPT_POST_PATTERNS, restore_gate_exempt

MAIN = Path(__file__).resolve().parent.parent / "app" / "main.py"


def _route_for_line(lines, lineno):
    for i in range(lineno - 1, max(0, lineno - 80), -1):
        m = re.search(r'@app\.(post|get|put|delete|patch)\("([^"]+)"', lines[i])
        if m:
            return m.group(1).upper(), m.group(2)
    raise AssertionError(f"no route decorator above line {lineno}")


def test_gate_exemptions_cover_every_credential_write_site():
    lines = MAIN.read_text(encoding="utf-8").splitlines()
    sites = set()
    for idx, line in enumerate(lines, 1):
        if "acquire_credential_write_lock(" in line:
            sites.add(_route_for_line(lines, idx))
    # 13 call sites, 10 distinct route templates at the time of writing; the
    # count pins drift in either direction.
    assert len(sites) == 11, sites
    for method, template in sites:
        if "{" in template:
            pattern = re.escape(template)
            pattern = re.sub(r"\\\{[^}]+\\\}", "[^/]+", pattern)
            sample = re.sub(r"\{[^}]+\}", "1", template)
            assert restore_gate_exempt(method, sample), f"{method} {template} not exempt"
        else:
            assert (method, template) in GATE_EXEMPT_EXACT, f"{method} {template} not exempt"


def test_restore_route_itself_is_exempt():
    assert restore_gate_exempt("POST", "/settings/backup/restore")


def test_exemption_table_has_no_unknown_entries():
    expected_exact = {
        ("POST", "/login"),
        ("GET", "/settings/password"),
        ("POST", "/settings/password"),
        ("POST", "/settings/telegram/bind-confirm"),
        ("POST", "/settings/accounts"),
        ("POST", "/settings/accounts/recovery-codes"),
        ("POST", "/login/recover"),
        ("POST", "/settings/backup/restore"),
    }
    assert GATE_EXEMPT_EXACT == expected_exact
    assert len(GATE_EXEMPT_POST_PATTERNS) == 2
    # Reads of ordinary pages must NOT be exempt - that is the whole point.
    assert not restore_gate_exempt("GET", "/")
    assert not restore_gate_exempt("GET", "/settings/accounts")
    assert not restore_gate_exempt("GET", "/employees")
    assert not restore_gate_exempt("POST", "/employees")


def test_parallel_shared_gates_do_not_serialize():
    """Two concurrent shared gate holders must both proceed (no reader-reader
    blocking); only the restore's exclusive lock excludes them."""
    from app.config import settings
    from app.restore_lock import RESTORE_LOCK_KEY, open_direct_connection

    if not settings.database_url.startswith("postgresql"):
        return  # no-op on non-PG unit-test backends
    c1 = open_direct_connection(autocommit=False)
    c2 = open_direct_connection(autocommit=False)
    try:
        c1.execute("SELECT pg_advisory_xact_lock_shared(%s)", (RESTORE_LOCK_KEY,))
        # Would hang if shared locks excluded each other; a second shared
        # acquisition on the same key returns immediately.
        c2.execute("SELECT pg_advisory_xact_lock_shared(%s)", (RESTORE_LOCK_KEY,))
    finally:
        c1.rollback(); c1.close()
        c2.rollback(); c2.close()
