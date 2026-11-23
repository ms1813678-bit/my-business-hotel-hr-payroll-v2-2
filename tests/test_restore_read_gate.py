"""Pin: the restore-read-gate exemption table must match the credential-write
lock call sites in app/main.py. A new gated write site without an exemption
(middleware shared lock + exclusive upgrade = self-deadlock) fails here."""
import re
from pathlib import Path

from app.restore_lock import GATE_EXEMPT_EXACT, GATE_EXEMPT_POST_PATTERNS, restore_gate_exempt

MAIN = Path(__file__).resolve().parent.parent / "app" / "main.py"


LOCK_MARKERS = ("acquire_credential_write_lock(", "_sensitive_reauth(")


def _route_segments():
    """(method, path, segment) per route: decorator line through the line
    before the next route decorator. Function-aware: a lock call inside a
    helper is attributed to the route whose handler segment calls it."""
    lines = MAIN.read_text(encoding="utf-8").splitlines()
    deco = [(i, re.search(r'@app\.(post|get|put|delete|patch)\("([^"]+)"', line))
            for i, line in enumerate(lines)]
    deco = [(i, m) for i, m in deco if m]
    # A route segment ends at the next route decorator OR the next top-level
    # def, so helpers defined between routes are not absorbed into the
    # preceding route's segment.
    boundaries = sorted({i for i, _ in deco} | {
        i for i, line in enumerate(lines) if re.match(r"^(def |async def )", line)
    })
    segments = []
    for i, m in deco:
        later = [b for b in boundaries if b > i]
        # The first boundary after the decorator is the route's own handler
        # def; the segment runs to the boundary AFTER it (the next route or
        # the next top-level helper).
        end = later[1] if len(later) > 1 else len(lines)
        segments.append((m.group(1).upper(), m.group(2), "\n".join(lines[i:end])))
    return segments


def _exempt(method, template):
    if (method, template) in GATE_EXEMPT_EXACT:
        return True
    if method == "POST" and "{" in template:
        sample = re.sub(r"\{[^}]+\}", "1", template)
        return restore_gate_exempt(method, sample)
    return False


def test_gate_exemptions_cover_every_credential_write_site():
    segments = _route_segments()
    gated = [(m, p) for m, p, seg in segments if any(k in seg for k in LOCK_MARKERS)]
    # The pin: every route whose handler reaches the exclusive credential
    # write lock (directly or via _sensitive_reauth) MUST be gate-exempt,
    # and no other route may be.
    for method, template in gated:
        assert _exempt(method, template), f"{method} {template} takes the write lock but is not gate-exempt"
    expected = {
        ("POST", "/login"),
        ("POST", "/settings/password"),
        ("POST", "/settings/telegram"),
        ("POST", "/settings/telegram/bind-link"),
        ("POST", "/settings/telegram/bind-confirm"),
        ("POST", "/settings/telegram/test"),
        ("POST", "/settings/accounts"),
        ("POST", "/settings/accounts/{account_id}/deactivate"),
        ("POST", "/settings/accounts/{account_id}/activate"),
        ("POST", "/settings/accounts/{account_id}/reset-password"),
        ("POST", "/settings/accounts/sessions/{session_id}/end"),
        ("POST", "/settings/accounts/recovery-codes"),
        ("POST", "/login/recover"),
        ("POST", "/settings/backup/restore"),
    }
    assert set(gated) == expected, set(gated) ^ expected


def test_restore_route_itself_is_exempt():
    assert restore_gate_exempt("POST", "/settings/backup/restore")


def test_exemption_table_has_no_unknown_entries():
    expected_exact = {
        ("POST", "/login"),
        ("POST", "/settings/password"),
        ("POST", "/settings/telegram"),
        ("POST", "/settings/telegram/bind-link"),
        ("POST", "/settings/telegram/bind-confirm"),
        ("POST", "/settings/telegram/test"),
        ("POST", "/settings/accounts"),
        ("POST", "/settings/accounts/recovery-codes"),
        ("POST", "/login/recover"),
        ("POST", "/settings/backup/restore"),
    }
    assert GATE_EXEMPT_EXACT == expected_exact
    # GET /settings/password is NOT exempt: it renders the Telegram binding
    # state, and a mid-window read of that state is exactly what the gate
    # exists to block (peer finding on bc74a88).
    assert not restore_gate_exempt("GET", "/settings/password")
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
