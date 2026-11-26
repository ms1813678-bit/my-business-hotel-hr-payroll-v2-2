"""5a: server-side role/route matrix, STATIC (AST) over pinned source snapshot (e60b73f, src/).
No DB, no HTTP, no role session executed: proves what guards are declared, not runtime behavior."""
import ast, hashlib, json, os, sys
HERE = os.path.dirname(__file__)
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from _route_matrix import all_rows

SRC = os.environ.get("G5A_SRC", os.path.join(ROOT, "app"))
rows = all_rows(SRC)
by_key = {f"{r['method']} {r['path']}": sorted(d for d in r["deps"] if d != "get_db") for r in rows}

PUBLIC = {"GET /language/{language}", "GET /health", "GET /login", "POST /login", "POST /logout",
          "GET /login/recover", "POST /login/recover"}
ADMIN = "require_system_admin"; PAY = "require_payroll_access"; AW = "require_attendance_write"
AR = "require_attendance_read"; LR = "require_leave_read"; ER = "require_employee_read"; ANY = "get_current_admin"


def roles_of(dep_names):
    """Role set of the effective (most restrictive) role guard on a route."""
    sets = [BUNDLES[d] for d in dep_names if d in BUNDLES]
    if not sets:
        return None
    out = set(sets[0])
    for s in sets[1:]: out &= s
    return out


def _bundles():
    t = ast.parse(open(os.path.join(SRC, "dependencies.py"), encoding="utf-8").read())
    consts, out = {}, {}
    for n in t.body:
        if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name):
            name = n.targets[0].id
            if isinstance(n.value, ast.Constant): consts[name] = n.value.value
            elif isinstance(n.value, ast.Call) and getattr(n.value.func, "id", "") == "require_roles":
                out[name] = {consts[a.id] for a in n.value.args}
    out["get_current_admin"] = {"system_admin", "finance", "assistant_manager"}
    return out


BUNDLES = _bundles()


def test_source_pin():
    h = hashlib.sha256(open(os.path.join(SRC, "dependencies.py"), "rb").read()).hexdigest()
    assert h.startswith("dca6efb196447579dd75")  # e60b73f app/dependencies.py


def test_bundles_match_owner_split():
    assert BUNDLES[ADMIN] == {"system_admin"}
    assert BUNDLES[PAY] == {"system_admin", "finance"}
    assert BUNDLES[AW] == {"system_admin", "assistant_manager"}      # finance never writes attendance/leave
    assert BUNDLES[AR] == {"system_admin", "finance", "assistant_manager"}
    assert BUNDLES[LR] == {"system_admin", "finance", "assistant_manager"}
    assert BUNDLES[ER] == {"system_admin", "finance"}


def test_every_route_authenticated_except_public_allowlist():
    unauth = {k for k, v in by_key.items() if not (set(v) & (set(BUNDLES)))}
    assert unauth == PUBLIC


def test_every_unsafe_method_has_csrf():
    missing = {k for k, v in by_key.items() if not k.startswith("GET ") and "csrf_protect" not in v}
    assert missing == set()


def _eff(prefix_pred):
    return {k: roles_of(v) for k, v in by_key.items() if prefix_pred(k.split(" ", 1)[1])}


def test_assistant_never_reaches_payroll_money_routes():
    pay = _eff(lambda p: p.startswith(("/payroll", "/advances", "/staff-actions", "/overtime", "/leave/payroll-day-handoff")))
    assert pay, "no payroll routes found"
    for k, r in pay.items():
        assert r is not None and "assistant_manager" not in r, k


def test_admin_only_surfaces():
    for k, r in _eff(lambda p: p.startswith(("/settings/accounts", "/settings/backup", "/settings/audit",
                                                "/settings/telegram", "/ai"))).items():
        assert r == {"system_admin"}, k
    for k in by_key:
        m, p = k.split(" ", 1)
        if p.startswith(("/employees", "/departments", "/branches", "/employee-data-change-requests")) and m == "POST":
            assert roles_of(by_key[k]) == {"system_admin"}, k


def test_finance_is_read_only_on_attendance_and_leave():
    for k, v in by_key.items():
        m, p = k.split(" ", 1)
        if p.startswith(("/attendance", "/security-roster", "/leave")) and m != "GET" and p != "/leave/payroll-day-handoff":
            r = roles_of(v)
            assert r is not None and "finance" not in r, k


def test_all_leave_workflow_routes_admin_or_attendance_writers():
    for k, v in by_key.items():
        if k.split(" ", 1)[1].startswith("/leave/") and "/payroll-day-handoff" not in k and k != "GET /leave/workbench":
            assert roles_of(v) in ({"system_admin"}, {"system_admin", "assistant_manager"}), k


def test_report_routes_gate_in_body():
    t = ast.parse(open(os.path.join(SRC, "main.py"), encoding="utf-8").read())
    src = open(os.path.join(SRC, "main.py"), encoding="utf-8").read()
    fn = {n.name: ast.get_source_segment(src, n) for n in ast.walk(t) if isinstance(n, ast.FunctionDef)}
    assert "_enforce_report_scope(selection, admin)" in fn["view_hr_report"]
    for name in ("export_hr_reports", "export_hr_reports_pdf"):
        assert 'admin.role != "system_admin" and not _custom_report_requested' in fn[name]
        assert "_enforce_report_scope(selection, admin)" in fn[name]
    assert "_REPORT_TYPES_BY_ROLE" in fn["reports_download_saved_file"] and "403" in fn["reports_download_saved_file"]
    assert "_monthly_allowed_types(admin)" in fn["reports_monthly_pdf"] and "rejected" in fn["reports_monthly_pdf"]


def test_report_type_scopes():
    src = open(os.path.join(SRC, "main.py"), encoding="utf-8").read()
    t = ast.parse(src)
    d = next(n for n in t.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "_REPORT_TYPES_BY_ROLE")
    text = ast.unparse(d.value)
    assert "ROLE_ASSISTANT: {REPORT_TYPE_ATTENDANCE}" in text
    fin = text.split("ROLE_FINANCE:")[1]
    assert "REPORT_TYPE_COMPREHENSIVE" not in fin and "REPORT_TYPE_PAYROLL" in fin


def test_golden_matrix_unchanged():
    """Any new/removed route or changed guard fails here and needs a conscious golden update."""
    golden = json.load(open(os.path.join(HERE, "golden_route_guards.json"), encoding="utf-8"))
    assert by_key == golden
