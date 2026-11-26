"""Windows check of app/launch_guard.py on a disposable runner. Synthetic folders only; reads no .env value.

Each case calls check_launch_context(root, cwd, environ). CORE cases must give the stated result or the
script exits 1. OBSERVE cases only print what the real Windows APIs returned (no pass/fail claim).
"""
import ctypes
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://x@127.0.0.1/none")
os.environ.setdefault("SESSION_SECRET", "ci-test-only-session-secret-not-for-production")
from app import launch_guard as lg  # noqa: E402

failures = []


def run(label, root, cwd, expect, core=True):
    result = lg.check_launch_context(root, cwd, {"DATABASE_URL": "x"})
    got = result.ok if expect in (True, False) else result.code
    if core and expect is False and result.code != lg.CODE_WRONG_CWD:
        got = "refused with code " + result.code  # a refusal for the wrong reason is a failure
    line = f"{'CORE   ' if core else 'OBSERVE'} {label}: ok={result.ok} code={result.code}"
    if core:
        good = got == expect
        line += f" expected={expect} -> {'PASS' if good else 'FAIL'}"
        if not good:
            failures.append(label)
    print(line, flush=True)


def short_path(path):
    buf = ctypes.create_unicode_buffer(32768)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, len(buf))
    return buf.value if n else None


def main():
    if os.name != "nt":
        print("FAIL: this probe must run on Windows (os.name != 'nt')")
        return 1
    print("platform", sys.platform, "os.name", os.name, "python", sys.version.split()[0], flush=True)
    base = Path(tempfile.mkdtemp(prefix="HotelHR Guard "))  # a space in the path on purpose
    root = base / "Install Root"
    (root / "app").mkdir(parents=True)
    for name in ("pyproject.toml", "alembic.ini"):
        (root / name).write_text("")
    (root / "app" / "__init__.py").write_text("")
    (root / ".env").write_text("SYNTHETIC=1\n")
    other = base / "Other Folder"
    other.mkdir()
    (other / ".env").write_text("SYNTHETIC=2\n")
    print("root", root, flush=True)

    run("cwd is the root", root, root, True)
    run("upper-case path", root, Path(str(root).upper()), True)
    run("lower-case path", root, Path(str(root).lower()), True)
    run("forward slashes", root, str(root).replace("\\", "/"), True)
    run("trailing backslash", root, str(root) + "\\", True)
    run("dot-dot segment", root, str(root / "app" / ".."), True)
    run("extended-length prefix", root, "\\\\?\\" + str(root), True)
    run("subfolder refused", root, root / "app", False)
    run("sibling folder with its own .env refused", root, other, False)
    run("empty cwd refused", root, "", False)
    run("NUL byte refused", root, str(root) + "\x00x", False)

    short = short_path(root)
    print("8.3 short path of root:", short, flush=True)
    if short and short.lower() != str(root).lower():
        run("8.3 short name of root", root, short, True)
    else:
        print("OBSERVE 8.3 short names unavailable on this volume; case not tested", flush=True)

    j_ok, j_bad = base / "JunctionToRoot", base / "JunctionToOther"
    for link, target in ((j_ok, root), (j_bad, other)):
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
        print("mklink", link.name, "rc", r.returncode, flush=True)
    if j_ok.exists():
        run("junction to the root accepted (same folder)", root, j_ok, True)
    if j_bad.exists():
        run("junction to another folder refused", root, j_bad, False)

    # OBSERVE: subst drive mapped to the root; what realpath returns for it
    drive = "Z:"
    r = subprocess.run(["subst", drive, str(root)], capture_output=True, text=True)
    if r.returncode == 0:
        run("subst drive mapped to root", root, drive + "\\", True, core=False)
        subprocess.run(["subst", drive, "/d"], capture_output=True, text=True)
    else:
        print("OBSERVE subst unavailable rc", r.returncode, flush=True)

    # real default install root and a real cwd change
    old = os.getcwd()
    os.chdir(root)
    try:
        result = lg.check_launch_context(root, None, {"DATABASE_URL": "x"})
        print("CORE   real os.getcwd() after chdir(root): ok=", result.ok, "-> ", "PASS" if result.ok else "FAIL", flush=True)
        if not result.ok:
            failures.append("real getcwd")
    finally:
        os.chdir(old)

    print("FAILURES:", failures if failures else "none", flush=True)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
