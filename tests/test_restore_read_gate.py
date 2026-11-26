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
    # Modular manual route reaches historical guard's exclusive lock.
    assert restore_gate_exempt('POST', '/leave/opening/review')
    assert not restore_gate_exempt('GET', '/leave/opening')


def test_restore_route_itself_is_exempt():
    assert restore_gate_exempt("POST", "/settings/backup/restore")


def test_exemption_table_has_no_unknown_entries():
    expected_exact = {
        ("POST", "/login"),
        ("POST", "/leave/opening/review"),
        ("POST", "/leave/departments/export"),
        ("POST", "/leave/departments/return"),
        ("POST", "/leave/departments/compare"),
    ("POST", "/leave/policies/preview"),
    ("POST", "/leave/policies/confirm"),
    ("POST", "/leave/policies/impact"),
    ("POST", "/leave/balance-explanation"),
        ("POST", "/leave/opening/policy-defaults"),
        ("POST", "/leave/opening/confirm-source"),
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
    assert len(GATE_EXEMPT_POST_PATTERNS) == 13
    assert restore_gate_exempt("POST", "/leave/month-review/reconciliation-snapshot")
    assert restore_gate_exempt("POST", "/leave/month-review/reconciliation-draft")
    assert not restore_gate_exempt("POST", "/leave/month-review/reconciliation-draft/x")
    assert restore_gate_exempt("POST", "/leave/month-review/register-review")
    assert restore_gate_exempt("POST", "/leave/month-review/register-confirm")
    assert not restore_gate_exempt("GET", "/leave/month-review")
    assert not restore_gate_exempt("POST", "/leave/month-review/register-confirm/other")
    earning='/leave/month-review/earning/'+('a'*64)+'/1'
    assert restore_gate_exempt('POST',earning+'/review')
    assert restore_gate_exempt('POST',earning+'/confirm')
    assert not restore_gate_exempt('GET',earning)
    assert not restore_gate_exempt('POST',earning+'/confirm/other')
    assert not restore_gate_exempt('POST','/leave/month-review/earning/bad/1/review')
    # Reads of ordinary pages must NOT be exempt - that is the whole point.
    assert restore_gate_exempt('POST','/attendance/shift-evidence/'+('a'*64)+'/1/review')
    assert restore_gate_exempt('POST','/attendance/shift-evidence/'+('a'*64)+'/1/confirm')
    assert not restore_gate_exempt('GET','/attendance/shift-evidence/'+('a'*64)+'/1')
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


def test_operational_router_canonical_validation_precedes_exclusive_service():
    """APIRouter-aware AST pin; main.py decorator scanner cannot see handlers."""
    import ast
    path=MAIN.parent/'shift_day_routes.py';tree=ast.parse(path.read_text())
    handlers={v.name:v for v in ast.walk(tree) if isinstance(v,ast.AsyncFunctionDef) and v.name in ('prepare','confirm')}
    assert set(handlers)=={'prepare','confirm'}
    for name,node in handlers.items():
        calls=[v for v in ast.walk(node) if isinstance(v,ast.Call)]
        canonical=min(v.lineno for v in calls if isinstance(v.func,ast.Name) and v.func.id=='canonical_operational_path')
        sensitive=[v.lineno for v in calls if isinstance(v.func,ast.Name) and v.func.id in ('prepare_evidence_review','consume_evidence_review','_identity')]
        assert sensitive and canonical<min(sensitive)
    for suffix in ('review','confirm'):
        path='/attendance/shift-evidence/'+('a'*64)+'/1/'+suffix
        assert restore_gate_exempt('POST',path)
        assert not restore_gate_exempt('POST',path+'\n')
        assert not restore_gate_exempt('POST',path.replace('/1/','/01/'))
        assert not restore_gate_exempt('POST',path.replace('a'*64,'A'*64))


def test_daily_overtime_gate_exemption_exact_and_monthly_shared_gate():
    assert not restore_gate_exempt('GET','/payroll/items/1/overtime-days')
    for action in ('review','confirm','monthly'):
        assert restore_gate_exempt('POST','/payroll/items/1/overtime-days/'+action)
        assert not restore_gate_exempt('POST','/payroll/items/1/overtime-days/'+action+'/other')
    assert not restore_gate_exempt('POST','/payroll/runs/1/approve')



def test_cash_out_exemption_is_only_canonical_self_guarded_post():
    assert restore_gate_exempt('POST', '/leave/cash-out/1/review')
    assert restore_gate_exempt('POST', '/leave/cash-out/1/confirm')
    for method, path in [('GET', '/leave/cash-out/1'), ('POST', '/leave/cash-out/01/review'),
        ('POST', '/leave/cash-out/1/review/other'), ('POST', '/leave/cash-out/x/confirm')]:
        assert not restore_gate_exempt(method, path)


def test_carry_consumption_exemptions_are_exact_posts_only():
    for action in ('review','confirm'):
        path='/leave/carry-consumption/1/'+action
        assert restore_gate_exempt('POST',path)
        assert not restore_gate_exempt('GET',path)
        assert not restore_gate_exempt('POST',path+'/extra')
        assert not restore_gate_exempt('POST',path.replace('/1/','/01/'))
        assert not restore_gate_exempt('POST',path.replace('/1/','/0/'))


def test_carry_expiry_exemptions_are_only_exact_exclusive_posts():
    for action in ('review','confirm'):
        path='/leave/carry-expiry/1/'+action
        assert restore_gate_exempt('POST',path)
        for method,changed in [('GET',path),('POST',path+'/extra'),('POST',path+'\n'),
                ('POST',path.replace('/1/','/01/')),('POST',path.replace('/1/','/0/')),
                ('POST',path.replace('/1/','/-1/'))]:
            assert not restore_gate_exempt(method,changed)
    assert not restore_gate_exempt('GET','/leave/carry-expiry/1')


def test_opening_rebind_exemptions_are_only_exact_exclusive_posts():
    for action in ('review','confirm'):
        path='/leave/opening-rebind/1/'+action
        assert restore_gate_exempt('POST',path)
        for method,changed in [('GET',path),('POST',path+'/extra'),('POST',path+'\n'),
                ('POST',path.replace('/1/','/01/')),('POST',path.replace('/1/','/0/')),
                ('POST',path.replace('/1/','/-1/'))]:
            assert not restore_gate_exempt(method,changed)
    assert not restore_gate_exempt('GET','/leave/opening-rebind/1')


def test_zero_decision_exemptions_are_only_exact_exclusive_posts():
    for action in ('review','confirm'):
        path='/leave/zero-decision/1/'+action
        assert restore_gate_exempt('POST',path)
        for method,changed in [('GET',path),('POST',path+'/extra'),('POST',path+'\n'),
                ('POST',path.replace('/1/','/01/')),('POST',path.replace('/1/','/0/')),
                ('POST',path.replace('/1/','/-1/'))]:
            assert not restore_gate_exempt(method,changed)
    assert not restore_gate_exempt('GET','/leave/zero-decision/1')
