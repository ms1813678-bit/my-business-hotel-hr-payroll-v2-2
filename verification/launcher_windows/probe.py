"""Windows check of app/launcher.py and scripts/windows/stop.ps1 on a disposable runner.

Synthetic: no real .env, no real database (DATABASE_URL points nowhere, so /health answers an error status, which
still proves the program is up). CORE steps must hold or the script exits 1. OBSERVE steps only print.
Not testable here: the Arabic message box (no interactive desktop), the browser, shortcut icons.
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PYW = Path(sys.executable).with_name("pythonw.exe")
failures = []


def core(label, ok, detail=""):
    print(f"CORE    {label}: {'PASS' if ok else 'FAIL'} {detail}", flush=True)
    if not ok:
        failures.append(label)


def observe(label, detail):
    print(f"OBSERVE {label}: {detail}", flush=True)


def listening():
    import socket
    with socket.socket() as c:
        c.settimeout(2)
        return c.connect_ex(("127.0.0.1", 8000)) == 0


def health():
    try:
        with urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:  # any HTTP status means a server answered
        return e.code
    except Exception:
        return None


DB_URL = os.environ.get("PROBE_DATABASE_URL")  # throwaway local cluster from the workflow; else a dead port
env = dict(os.environ, DATABASE_URL=DB_URL or "postgresql+psycopg://x@127.0.0.1:1/none",
           SESSION_SECRET="ci-test-only-session-secret-not-for-production", PYTHONPATH=str(REPO))
core("pythonw.exe exists next to python.exe", PYW.is_file(), str(PYW))

# 1. Wrong folder: refused, exit 2, nothing listens on 8000. The message box may block on a runner with
#    no desktop, so a timeout is only observed (the process is killed).
other = tempfile.mkdtemp(prefix="notthefolder")
p = subprocess.Popen([str(PYW), "-m", "app.launcher"], cwd=other, env=env)
try:
    code = p.wait(timeout=20)
    core("wrong folder exits 2", code == 2, f"exit={code}")
except subprocess.TimeoutExpired:
    p.kill()
    observe("wrong folder", "did not exit in 20s (message box probably waiting); killed")
core("nothing listening after wrong-folder refusal", health() is None)

# 2. Right folder under pythonw (stdout/stderr are None): the server must come up.
srv = subprocess.Popen([str(PYW), "-m", "app.launcher"], cwd=str(REPO), env=env)
status = None
end = time.monotonic() + 240
while time.monotonic() < end:
    status = health()
    if status is not None or srv.poll() is not None:
        break
    time.sleep(1)
if DB_URL:
    core("with the local throwaway database /health returns 200", status == 200, f"status={status}")
observe("port 8000 accepts TCP connections", listening())
core("pythonw launcher serves on 127.0.0.1:8000 (any HTTP status; no DB so not 200)", status is not None,
     f"status={status} exit={srv.poll()}")
log = REPO / "var" / "launcher.log"
core("log file written under var", log.is_file() and log.stat().st_size > 0)
if log.is_file():
    observe("log tail", log.read_text(encoding="utf-8", errors="replace")[-4000:].replace("\n", " | "))

# 3. stop.ps1 ends it.
r = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(REPO / "scripts" / "windows" / "stop.ps1")], capture_output=True, text=True, timeout=60)
observe("stop.ps1", f"exit={r.returncode} {r.stderr.strip()[:200]}")
for _ in range(15):
    if srv.poll() is not None:
        break
    time.sleep(1)
core("stop.ps1 ended the launcher process", srv.poll() is not None, f"exit={srv.poll()}")
core("port closed after stop", health() is None)
if srv.poll() is None:
    srv.kill()
sys.exit(1 if failures else 0)
