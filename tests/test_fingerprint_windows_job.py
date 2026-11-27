"""Windows job-object adapter for the fingerprint parser. Behavior tests run on Windows only;
the static contract tests run everywhere."""
import os, subprocess, sys, time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
windows_only = pytest.mark.skipif(os.name != "nt", reason="UNRAN: Windows job objects exist only on Windows")


def test_limits_match_the_posix_worker_bounds():
    from app import fingerprint_windows_job as j
    src = (ROOT / "app" / "fingerprint_parse_worker.py").read_text(encoding="utf-8")
    assert j.CPU_SECONDS == 2 and "RLIMIT_CPU,(2,2)" in src
    assert j.MEMORY_BYTES == 512 * 1024 * 1024 and "RLIMIT_AS,(512*1024*1024" in src
    assert j.OUTPUT_LIMIT == 4_000_000 and "RLIMIT_FSIZE,(4_000_000" in src
    assert j.WALL_SECONDS == 3
    from app import fingerprint_bounded_parse as parser
    assert parser.OUTPUT_BYTES == j.OUTPUT_LIMIT


def test_required_job_flags_cover_time_memory_processes_and_kill_on_close():
    from app import fingerprint_windows_job as j
    for flag in (j.JOB_OBJECT_LIMIT_PROCESS_TIME, j.JOB_OBJECT_LIMIT_ACTIVE_PROCESS,
                 j.JOB_OBJECT_LIMIT_PROCESS_MEMORY, j.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE):
        assert j.REQUIRED_FLAGS & flag
    assert j.UI_RESTRICTIONS_ALL == 0xFF


def test_worker_refuses_to_run_unless_isolation_is_verified():
    src = (ROOT / "app" / "fingerprint_parse_worker.py").read_text(encoding="utf-8")
    assert "verify_current_process_is_bounded" in src and "isolation is not in force" in src
    assert src.index("verify_current_process_is_bounded()") < src.index("parse_fingerprint_evidence(payload")


def test_unknown_platform_stays_unavailable(monkeypatch):
    import app.fingerprint_bounded_parse as parser
    monkeypatch.setattr(parser, "_operational_platform", lambda: "unsupported-os")
    with pytest.raises(parser.FingerprintPlatformUnavailable):
        parser.require_operational_parser_platform()


def test_gate_accepts_posix_and_nt_and_nothing_else(monkeypatch):
    import app.fingerprint_bounded_parse as parser
    for name in ("posix", "nt"):
        monkeypatch.setattr(parser, "_operational_platform", lambda name=name: name)
        parser.require_operational_parser_platform()
    for name in ("java", "unsupported-os", ""):
        monkeypatch.setattr(parser, "_operational_platform", lambda name=name: name)
        with pytest.raises(parser.FingerprintPlatformUnavailable):
            parser.require_operational_parser_platform()


def test_route_gates_use_the_same_function():
    for name in ("shift_day_routes.py", "leave_month_review_routes.py"):
        src = (ROOT / "app" / name).read_text(encoding="utf-8")
        assert "require_operational_parser_platform()" in src and "os.name" not in src


def test_no_unbounded_fallback_in_parse_path():
    src = (ROOT / "app" / "fingerprint_bounded_parse.py").read_text(encoding="utf-8")
    nt = src[src.index("if os.name=='nt':"):src.index("with tempfile.TemporaryFile()")]
    assert "subprocess.run" not in nt and "JobError" in nt and "FingerprintPlatformUnavailable" in nt


@windows_only
def test_worker_outside_a_job_is_refused():
    code = "from app.fingerprint_windows_job import verify_current_process_is_bounded as v;import sys;sys.exit(0 if v() else 3)"
    run = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)}, timeout=30)
    # The test process itself may run inside a CI job; only an unlimited job must be refused.
    assert run.returncode == 3


@windows_only
def test_worker_inside_the_job_sees_the_limits():
    from app import fingerprint_windows_job as j
    code, out = j.run_bounded_worker(b"", ROOT, ("-c", "from app.fingerprint_windows_job import verify_current_process_is_bounded as v;import sys;sys.stdout.write('yes' if v() else 'no')"))
    assert code == 0 and out == b"yes"


@windows_only
def test_endless_loop_is_killed_within_budget():
    # The guarantee: a busy loop never returns a result and is gone within the budget.
    # Which limit stops it (2 s CPU limit or 3 s wall limit) is measured and reported, not asserted.
    import warnings
    from app import fingerprint_windows_job as j
    started = time.monotonic()
    result = None
    try:
        result = j.run_bounded_worker(b"", ROOT, ("-c", "while True: pass"))
        ended = "job CPU limit (process exited with code %s, output %d bytes)" % (result[0], len(result[1]))
    except subprocess.TimeoutExpired:
        ended = "wall budget (TimeoutExpired)"
    elapsed = time.monotonic() - started
    warnings.warn("endless-loop worker ended by %s after %.2f s" % (ended, elapsed))
    if result is not None:
        assert result[0] != 0 and result[1] == b""
    assert elapsed < 6


@windows_only
def test_sleeping_worker_hits_wall_budget():
    from app import fingerprint_windows_job as j
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        j.run_bounded_worker(b"", ROOT, ("-c", "import time; time.sleep(30)"))
    assert time.monotonic() - started < 6


@windows_only
def test_memory_hog_is_refused():
    from app import fingerprint_windows_job as j
    code, out = j.run_bounded_worker(b"", ROOT, ("-c", "x = bytearray(900*1024*1024); print(len(x))"))
    assert code != 0 and out.strip() != b"943718400"


@windows_only
def test_output_flood_is_cut_off():
    from app import fingerprint_windows_job as j
    started = time.monotonic()
    code, out = j.run_bounded_worker(b"", ROOT, ("-c", "import sys\nwhile True: sys.stdout.buffer.write(b'x'*65536); sys.stdout.buffer.flush()"))
    assert code != 0 and len(out) <= j.OUTPUT_LIMIT + 65536 * 2 and time.monotonic() - started < 6


@windows_only
def test_extra_process_cannot_be_spawned():
    from app import fingerprint_windows_job as j
    limit = j.active_process_limit()
    code, out = j.run_bounded_worker(b"", ROOT, ("-c", "import subprocess,sys\nps=[]\ntry:\n  for _ in range(4): ps.append(subprocess.Popen([sys.executable,'-c','import time;time.sleep(5)']))\n  print('spawned',len(ps))\nexcept OSError:\n  print('blocked',len(ps))"))
    assert out.startswith(b"blocked") or code != 0, out
    assert limit in (1, 2)


@windows_only
def test_stdin_payload_round_trips():
    from app import fingerprint_windows_job as j
    code, out = j.run_bounded_worker(b"hello-" * 1000, ROOT, ("-c", "import sys;d=sys.stdin.buffer.read();sys.stdout.buffer.write(str(len(d)).encode())"))
    assert code == 0 and out == b"6000"
