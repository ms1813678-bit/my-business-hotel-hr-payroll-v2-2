"""Windows isolation adapter for the fingerprint parser worker.

Same bounds as the POSIX path (CPU 2 s, memory 512 MiB, wall 3 s, output 4 MB, JSON only, no pickle,
kill on timeout), enforced by a Windows Job Object that the worker is placed in BEFORE it runs a single
instruction (created suspended, assigned, then resumed). Fails closed: if any step cannot be completed the
worker is never resumed and FingerprintPlatformUnavailable-style refusal is raised by the caller.
"""
import ctypes, os, subprocess, sys, threading
from ctypes import wintypes
from pathlib import Path

WALL_SECONDS = 3
CPU_SECONDS = 2
MEMORY_BYTES = 512 * 1024 * 1024
OUTPUT_LIMIT = 4_000_000
INPUT_LIMIT = 16_000_000

JOB_OBJECT_LIMIT_PROCESS_TIME = 0x2
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x8
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x100
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
REQUIRED_FLAGS = (JOB_OBJECT_LIMIT_PROCESS_TIME | JOB_OBJECT_LIMIT_ACTIVE_PROCESS
                  | JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
JobObjectBasicUIRestrictions = 4
JobObjectExtendedLimitInformation = 9
UI_RESTRICTIONS_ALL = 0xFF
CREATE_SUSPENDED = 0x4
CREATE_NO_WINDOW = 0x08000000


class JobError(RuntimeError):
    """The isolation could not be established or enforced. Nothing was parsed."""


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
        "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class BASIC_LIMIT(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD)]


class EXTENDED_LIMIT(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", BASIC_LIMIT), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class UI_RESTRICTIONS(ctypes.Structure):
    _fields_ = [("UIRestrictionsClass", wintypes.DWORD)]


def _kernel32():
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateJobObjectW.restype = wintypes.HANDLE
    k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    k.SetInformationJobObject.restype = wintypes.BOOL
    k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID]
    k.QueryInformationJobObject.restype = wintypes.BOOL
    k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    k.AssignProcessToJobObject.restype = wintypes.BOOL
    k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.TerminateJobObject.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    return k


def _in_venv():
    return getattr(sys, "base_prefix", sys.prefix) != sys.prefix


def active_process_limit():
    # A venv python.exe on Windows is a launcher that starts the real interpreter as one child.
    return 2 if _in_venv() else 1


def worker_executable():
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        console = exe.with_name("python.exe")
        if not console.exists():
            raise JobError("Console Python not found next to pythonw.")
        return str(console)
    return str(exe)


def worker_environment(root):
    system_root = os.environ.get("SYSTEMROOT") or r"C:\Windows"
    return {"SYSTEMROOT": system_root, "PATH": system_root + r"\System32", "PYTHONPATH": str(root),
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def verify_current_process_is_bounded():
    """Called by the worker itself. Refuses unless it runs inside a job with the required limits."""
    if os.name != "nt":
        return False
    try:
        k = _kernel32()
        info = EXTENDED_LIMIT()
        if not k.QueryInformationJobObject(None, JobObjectExtendedLimitInformation, ctypes.byref(info),
                                           ctypes.sizeof(info), None):
            return False
        b = info.BasicLimitInformation
        return ((b.LimitFlags & REQUIRED_FLAGS) == REQUIRED_FLAGS
                and 0 < b.PerProcessUserTimeLimit <= CPU_SECONDS * 10_000_000
                and 0 < info.ProcessMemoryLimit <= MEMORY_BYTES
                and 0 < b.ActiveProcessLimit <= 2)
    except Exception:
        return False


def _make_job(k):
    job = k.CreateJobObjectW(None, None)
    if not job:
        raise JobError("CreateJobObject failed: %d" % ctypes.get_last_error())
    info = EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = REQUIRED_FLAGS
    info.BasicLimitInformation.PerProcessUserTimeLimit = CPU_SECONDS * 10_000_000
    info.BasicLimitInformation.ActiveProcessLimit = active_process_limit()
    info.ProcessMemoryLimit = MEMORY_BYTES
    if not k.SetInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)):
        err = ctypes.get_last_error(); k.CloseHandle(job)
        raise JobError("SetInformationJobObject(limits) failed: %d" % err)
    ui = UI_RESTRICTIONS(UI_RESTRICTIONS_ALL)
    if not k.SetInformationJobObject(job, JobObjectBasicUIRestrictions, ctypes.byref(ui), ctypes.sizeof(ui)):
        err = ctypes.get_last_error(); k.CloseHandle(job)
        raise JobError("SetInformationJobObject(ui) failed: %d" % err)
    return job


WORKER_ARGS = ("-m", "app.fingerprint_parse_worker")


def run_bounded_worker(encoded, root, worker_args=WORKER_ARGS):
    """Run `python -m app.fingerprint_parse_worker` in a job. Returns (returncode, output bytes).

    Raises subprocess.TimeoutExpired on the wall budget, ValueError-free JobError if isolation fails.
    """
    if os.name != "nt":
        raise JobError("Windows adapter used on a non-Windows platform.")
    if len(encoded) > INPUT_LIMIT:
        raise JobError("Input exceeds budget.")
    k = _kernel32()
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    job = _make_job(k)
    proc = None
    try:
        proc = subprocess.Popen([worker_executable(), *worker_args],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                env=worker_environment(root), cwd=str(root), close_fds=True,
                                creationflags=CREATE_SUSPENDED | CREATE_NO_WINDOW)
        if not k.AssignProcessToJobObject(job, int(proc._handle)):
            raise JobError("AssignProcessToJobObject failed: %d" % ctypes.get_last_error())
        status = ntdll.NtResumeProcess(int(proc._handle))
        if status != 0:
            raise JobError("NtResumeProcess failed: %d" % status)
        out = bytearray(); state = {"over": False}

        def feed():
            try:
                proc.stdin.write(encoded); proc.stdin.close()
            except OSError:
                pass

        def drain():
            try:
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        return
                    out.extend(chunk)
                    if len(out) > OUTPUT_LIMIT:
                        state["over"] = True
                        k.TerminateJobObject(job, 1)
                        return
            except (OSError, ValueError):
                return

        writer = threading.Thread(target=feed, daemon=True); reader = threading.Thread(target=drain, daemon=True)
        writer.start(); reader.start()
        try:
            code = proc.wait(timeout=WALL_SECONDS)
        except subprocess.TimeoutExpired:
            k.TerminateJobObject(job, 1)
            proc.wait()
            raise
        reader.join(timeout=1)
        if reader.is_alive():
            k.TerminateJobObject(job, 1)
            raise subprocess.TimeoutExpired("fingerprint worker", WALL_SECONDS)
        if state["over"]:
            return 1, bytes(out)
        return code, bytes(out)
    finally:
        if proc is not None:
            k.TerminateJobObject(job, 1)
            for stream in (proc.stdin, proc.stdout):
                try:
                    stream.close()
                except Exception:
                    pass
        k.CloseHandle(job)
