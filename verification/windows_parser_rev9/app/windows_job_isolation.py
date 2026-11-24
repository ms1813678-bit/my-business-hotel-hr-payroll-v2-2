"""CANDIDATE rev 9. Windows Job Object isolation for the untrusted attendance parser.

STATUS: written and unit-tested only against a fake API on Linux. It has NOT been
run on Windows. No claim is made that any limit works until the Windows-only
tests in tests/test_windows_job_isolation.py pass on a real Windows device.

Fail closed: on any non-Windows platform, or any error before/while running, the
function raises IsolationError (a ValueError). There is no unisolated fallback.
Stdlib ctypes only. No DB access, no pickle.

REV 9 = post-allocation OWNERSHIP (fix for a rev 8 gap: WinApi.create_job allocated a job, then on a
SetInformationJobObject failure skipped CloseHandle on error 6 or called close unchecked on other
errors, then raised: the identity was lost with no retained entry and no capacity accounting).
RULES: (a) adapters ALLOCATE ONLY and never close; nothing that can fail runs after an allocation
inside an adapter (create_job / configure_job split; create_suspended's attribute-list delete cannot
mask the two handles). (b) every owner (_Handle) is created EMPTY before the allocating call and adopts
the raw value at once (_Handle.adopt). (c) every failure after allocation is handled by owners: error 6
poisons (quarantine), other errors close through the owner, a failed close quarantines, and the
cleanup phase moves whatever is still open into a retained entry that converts the admission
reservation atomically. (d) if cleanup itself raises, everything still open is retained with exit and
termination marked UNCONFIRMED (reaper re-kills before any close). (e) pid_state's diagnostic handle is
owned and a failed close is retained/counted. (f) feed/drain use owner.value(), never a raw number.
RESIDUAL LIMITS: asynchronous interpreter exceptions (KeyboardInterrupt, MemoryError) landing between
an adapter return and adopt(); a retained quarantined entry is never freed in this process.

REV 8 = error classification at the ADAPTER boundary (fix for a rev 7 defect: WinApi.cancel_io,
cancel_sync_io, terminate_job and terminate_process returned bool and dropped GetLastError, so
the owner wrappers that quarantine on InvalidHandleError never saw it from the real adapter;
fakes that raised InvalidHandleError directly hid this).
ADAPTER CONTRACT, every WinApi method that can fail on a handle: FALSE/failure is classified
HERE, once, from GetLastError read immediately after the call (use_last_error, thread-local):
  ERROR_INVALID_HANDLE (6)  -> raise InvalidHandleError (owner quarantines the identity).
  ERROR_NOT_FOUND (1168)    -> cancel_io / cancel_sync_io only: no cancellable operation; ordinary,
                               returns False, nothing is quarantined.
  any other error           -> False (cancel, terminate: advisory/checked by callers) or
                               IsolationError (wait, exit code, read, write, resume, assign,
                               uninheritable). Never silently True.
Owners (_Handle, _ThreadSlot, _terminate_tree, _reap_one, run) catch InvalidHandleError BEFORE any
generic except, so it can no longer be swallowed. Where an operation names TWO identities
(AssignProcessToJobObject: job and process) and reports 6, BOTH are quarantined. CloseHandle is the
exception by design: any FALSE already quarantines (no classification needed). Reviewed and
unchanged: wait, exit_code, read, write (already raise on 6); handle_state/pid_state (any failure
is 'unknown'); open_current_thread, create_* (no owned identity exists yet).

REV 7 = identity-quarantine invariant (replaces the rev 6 "retry close up to 4 times").
INVARIANT: a numeric handle identity is used for NOTHING (close, cancel, wait, terminate,
exit-code query) after the first ambiguous failure on it. Ambiguous = CloseHandle returned
FALSE or raised, or any operation on it reported ERROR_INVALID_HANDLE. Such a handle is
QUARANTINED: it keeps counting against capacity (its entry is never removed), nothing is ever
called on its value again, and the run fails closed. A later TRUE from CloseHandle on the same
number would not prove the ORIGINAL object closed (the number may name a different object by
then), so retry is not offered at all. Whether a failed CloseHandle left the object open or
invalid is NOT knowable from the call; that is a residual limit, not something this file fixes.
The old cap of 4 is gone (it was a count, the defect was the retry). Quarantined entries are
never freed in this process: a restart of the worker host is the only recovery (limit).

REV 6 = lifecycle/ownership fix (see the "REV 6" block in the proposal). Three
verified gaps in rev 5, one state machine:
  1. A handle whose CloseHandle FAILED is never forgotten. _Handle/_ThreadSlot keep
     the identity on failure (quarantine) and retry; at most MAX_CLOSE_ATTEMPTS real
     CloseHandle calls per handle, after which it is ABANDONED: identity kept, never
     closed again (a failed CloseHandle may mean the value is already invalid and
     could be reused), entry stays counted against the cap. Any close failure blocks success.
  2. Explicit assigned/unassigned child state. An unassigned child (assign failed) is
     contained only by direct TerminateProcess: it is retried on every reap until exit
     is confirmed. The empty job is never treated as containment.
  3. Admission reserves a slot (retained + in-flight <= MAX_RETAINED) under one lock, and
     each retained entry has an exclusive per-entry reap lock, so no handle is closed
     twice and no run can overshoot the cap.
Still NOT provided: original proof; no-surviving-descendants / host-exit proof.

REV 5 = whole-file review of lifetime, identity and return-value contracts.
Two defects reported against rev 4 were verified in the rev 4 file, and the
review found more of the same kind. Summary (details in the proposal):

DESIGN CHANGE FROM REV 4 (explicit): the rev 4 "emergency close" is REMOVED.
Rev 4 closed the job/process/thread handles in `finally` even when exit was not
confirmed. Rev 5 never closes a handle that controls a child (process, primary
thread, job) unless exit of that child is confirmed AND TerminateJobObject
reported success. Otherwise the run fails and the handles move to a module-level
RETAINED OWNER (`_RETAINED`): they stay open for the life of the host process
(so the KILL_ON_JOB_CLOSE backstop still fires when the host exits), later runs
try to reap them (`reap_retained`), and they are closed only after confirmed
exit. The leak is bounded: at MAX_RETAINED entries new runs are refused.
I/O pipe handles and per-thread cancel handles are closed only after the thread
that uses them has ended (unchanged rule, now applied uniformly).

Identity fix: I/O threads no longer publish a native thread id for the parent to
OpenThread later (ids are reusable system-wide; is_alive-then-OpenThread races).
Each I/O thread duplicates a real handle to ITSELF (DuplicateHandle on the
current-thread pseudo handle, THREAD_TERMINATE) as its very first action, before
any I/O, and publishes it in a slot owned by the parent. A handle pins the thread
object, so it cannot be reused; the parent closes it only after the thread ended.

Return-value fixes: TerminateJobObject / TerminateProcess results are checked
(a failed job kill is a failed run, even after a normal exit); every CloseHandle
result is checked; ResumeThread must return exactly 1; ReadFile EOF is only
ERROR_BROKEN_PIPE (a successful zero-byte read is not EOF); a sizing call that
yields 0 is refused; an I/O thread that fails to start is a clean IsolationError.
"""
import ctypes, os, subprocess, sys, threading, time

# ---- limits (contract: 3 s wall, 2 s CPU, 512 MiB, 4 MB output) -------------
# TIME BUDGET (what the numbers really mean):
#   WALL_SECONDS is measured from the START of run_in_job (before job creation,
#   pipes and CreateProcessW; AFTER the pre-run reap of retained children) to child
#   exit. If the budget is spent when setup finishes, the child is never resumed.
#   After the wall deadline each cleanup phase runs AT MOST ONCE and is bounded:
#     one poll slice (POLL_SECONDS), ONE confirmation wait (KILL_WAIT_SECONDS),
#     ONE I/O stop window (JOIN_WAIT_SECONDS, shared by both threads).
#   Ceiling = WALL + POLL + KILL_WAIT + JOIN_WAIT (default 3 + .05 + 2 + 1 = 6.05 s).
#   There is no second wait and no second stop window (rev 4's emergency close
#   added up to 2 s and a second stop; it is gone). Tests pin the phase counts.
#   NOT bounded by us: one blocking Win32 call during setup (e.g. CreateProcessW),
#   the pre-run reap (<= MAX_RETAINED * REAP_WAIT_SECONDS = 0.4 s by default) and
#   thread scheduling. A Linux test checks the arithmetic against a fake only.
WALL_SECONDS = 3.0
POLL_SECONDS = 0.05
KILL_WAIT_SECONDS = 2.0
JOIN_WAIT_SECONDS = 1.0
CANCEL_AFTER_SECONDS = 0.25     # grace before cancelling a still-blocked I/O thread
REAP_WAIT_SECONDS = 0.1         # per retained entry, per reap attempt
ERROR_INVALID_HANDLE = 6        # any operation reporting this poisons that identity (see _Handle)
MAX_RETAINED = 4                # unreaped children allowed before new runs are refused
MAX_ZERO_READS = 1000           # consecutive successful 0-byte reads tolerated
MAX_CMDLINE_CHARS = 32767       # CreateProcessW lpCommandLine limit incl. NUL (Microsoft Learn)
CPU_SECONDS = 2
MEMORY_BYTES = 512 * 1024 * 1024
OUTPUT_BYTES = 4_000_000
INPUT_BYTES = 16_000_000
TICKS_PER_SECOND = 10_000_000          # PerProcessUserTimeLimit is in 100 ns ticks

# ---- Win32 constants (flag values per Microsoft docs / SDK headers) ---------
JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
REQUIRED_FLAGS = (JOB_OBJECT_LIMIT_PROCESS_TIME | JOB_OBJECT_LIMIT_ACTIVE_PROCESS |
                  JOB_OBJECT_LIMIT_PROCESS_MEMORY | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION |
                  JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)
JobObjectExtendedLimitInformation = 9
CREATE_SUSPENDED = 0x00000004
CREATE_UNICODE_ENVIRONMENT = 0x00000400
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
CREATE_NO_WINDOW = 0x08000000
STARTF_USESTDHANDLES = 0x00000100
PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
HANDLE_FLAG_INHERIT = 0x1
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 0x102
ERROR_BROKEN_PIPE = 109
ERROR_OPERATION_ABORTED = 995
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87
ERROR_NOT_FOUND = 1168
THREAD_TERMINATE = 0x0001      # access needed for CancelSynchronousIo (documented)
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
GENERIC_READ_WRITE_NUL = 0x40000000 | 0x80000000
OPEN_EXISTING = 3

# pid_state results
PID_ALIVE = 'alive'
PID_DEAD = 'dead'
PID_UNKNOWN = 'unknown'


class IsolationError(ValueError):
    pass




class InvalidHandleError(IsolationError):
    """An operation reported ERROR_INVALID_HANDLE: the identity it used is no longer trustworthy."""

class IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in (
        'ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
        'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]


class JobBasicLimit(ctypes.Structure):  # JOBOBJECT_BASIC_LIMIT_INFORMATION
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),   # LARGE_INTEGER
                ('PerJobUserTimeLimit', ctypes.c_int64),
                ('LimitFlags', ctypes.c_uint32),               # DWORD
                ('MinimumWorkingSetSize', ctypes.c_size_t),    # SIZE_T
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', ctypes.c_uint32),
                ('Affinity', ctypes.c_size_t),                 # ULONG_PTR
                ('PriorityClass', ctypes.c_uint32),
                ('SchedulingClass', ctypes.c_uint32)]


class JobExtendedLimit(ctypes.Structure):  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    _fields_ = [('BasicLimitInformation', JobBasicLimit),
                ('IoInfo', IoCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t),
                ('PeakJobMemoryUsed', ctypes.c_size_t)]


class StartupInfoW(ctypes.Structure):
    _fields_ = [('cb', ctypes.c_uint32), ('lpReserved', ctypes.c_void_p),
                ('lpDesktop', ctypes.c_void_p), ('lpTitle', ctypes.c_void_p),
                ('dwX', ctypes.c_uint32), ('dwY', ctypes.c_uint32),
                ('dwXSize', ctypes.c_uint32), ('dwYSize', ctypes.c_uint32),
                ('dwXCountChars', ctypes.c_uint32), ('dwYCountChars', ctypes.c_uint32),
                ('dwFillAttribute', ctypes.c_uint32), ('dwFlags', ctypes.c_uint32),
                ('wShowWindow', ctypes.c_uint16), ('cbReserved2', ctypes.c_uint16),
                ('lpReserved2', ctypes.c_void_p), ('hStdInput', ctypes.c_void_p),
                ('hStdOutput', ctypes.c_void_p), ('hStdError', ctypes.c_void_p)]


class StartupInfoExW(ctypes.Structure):
    _fields_ = [('StartupInfo', StartupInfoW), ('lpAttributeList', ctypes.c_void_p)]


class ProcessInformation(ctypes.Structure):
    _fields_ = [('hProcess', ctypes.c_void_p), ('hThread', ctypes.c_void_p),
                ('dwProcessId', ctypes.c_uint32), ('dwThreadId', ctypes.c_uint32)]


class SecurityAttributes(ctypes.Structure):
    _fields_ = [('nLength', ctypes.c_uint32), ('lpSecurityDescriptor', ctypes.c_void_p),
                ('bInheritHandle', ctypes.c_int32)]


def build_limits(cpu_seconds=CPU_SECONDS, memory_bytes=MEMORY_BYTES, active_process_limit=1):
    """Limit set. active_process_limit=0 omits JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    (used ONLY by the orphan-kill acceptance test, so the grandchild can exist)."""
    info = JobExtendedLimit()
    b = info.BasicLimitInformation
    flags = REQUIRED_FLAGS
    if active_process_limit:
        b.ActiveProcessLimit = active_process_limit
    else:
        flags &= ~JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    b.LimitFlags = flags
    b.PerProcessUserTimeLimit = cpu_seconds * TICKS_PER_SECOND
    info.ProcessMemoryLimit = memory_bytes
    return info


def build_environment(parent_env=None, pythonpath=''):
    """Allowlist only. Nothing is copied from the parent except SYSTEMROOT."""
    parent_env = os.environ if parent_env is None else parent_env
    root = parent_env.get('SYSTEMROOT') or parent_env.get('SystemRoot') or ''
    env = {'PYTHONPATH': pythonpath, 'PYTHONUTF8': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    if root:
        env['SYSTEMROOT'] = root
        env['PATH'] = root + '\\System32'
    return env


def make_command_line_buffer(cmdline):
    """Mutable, owned UTF-16 buffer for CreateProcessW lpCommandLine (NUL-terminated)."""
    if not cmdline or any((not isinstance(a, str)) or '\0' in a for a in cmdline):
        raise IsolationError('Invalid command line.')
    text = subprocess.list2cmdline(cmdline)
    if len(text) + 1 > MAX_CMDLINE_CHARS:
        raise IsolationError('Command line too long.')
    return ctypes.create_unicode_buffer(text)


def environment_block(env):
    # UTF-16 'K=V\0' items sorted case-insensitively, final extra NUL.
    items = sorted(env.items(), key=lambda kv: kv[0].upper())
    for k, v in items:
        if not k or '=' in k or '\0' in k or '\0' in v:
            raise IsolationError('Invalid environment entry.')
    text = ''.join(f'{k}={v}\0' for k, v in items) + '\0'
    return text.encode('utf-16-le')


class WinApi:
    """Thin ctypes layer over kernel32. Only constructible on Windows."""

    def __init__(self):
        if sys.platform != 'win32':
            raise IsolationError('Windows job isolation is unavailable on this platform.')
        k = ctypes.WinDLL('kernel32', use_last_error=True)
        V, U32, B = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32
        P = ctypes.POINTER
        def sig(name, res, *args):
            f = getattr(k, name); f.restype = res; f.argtypes = list(args); return f
        self.CreateJobObjectW = sig('CreateJobObjectW', V, V, ctypes.c_wchar_p)
        self.SetInformationJobObject = sig('SetInformationJobObject', B, V, B, V, U32)
        self.AssignProcessToJobObject = sig('AssignProcessToJobObject', B, V, V)
        self.TerminateJobObject = sig('TerminateJobObject', B, V, U32)
        self.CreateProcessW = sig('CreateProcessW', B, ctypes.c_wchar_p, V, V, V, B, U32, V,
                                  ctypes.c_wchar_p, V, P(ProcessInformation))
        self.ResumeThread = sig('ResumeThread', U32, V)
        self.WaitForSingleObject = sig('WaitForSingleObject', U32, V, U32)
        self.GetExitCodeProcess = sig('GetExitCodeProcess', B, V, P(U32))
        self.TerminateProcess = sig('TerminateProcess', B, V, U32)
        self.CloseHandle = sig('CloseHandle', B, V)
        self.CancelIoEx = sig('CancelIoEx', B, V, V)
        self.CancelSynchronousIo = sig('CancelSynchronousIo', B, V)
        self.GetCurrentThread = sig('GetCurrentThread', V)
        self.GetCurrentProcess = sig('GetCurrentProcess', V)
        self.DuplicateHandle = sig('DuplicateHandle', B, V, V, V, P(V), U32, B, U32)
        self.CreatePipe = sig('CreatePipe', B, P(V), P(V), P(SecurityAttributes), U32)
        self.SetHandleInformation = sig('SetHandleInformation', B, V, U32, U32)
        self.ReadFile = sig('ReadFile', B, V, V, U32, P(U32), V)
        self.WriteFile = sig('WriteFile', B, V, V, U32, P(U32), V)
        self.CreateFileW = sig('CreateFileW', V, ctypes.c_wchar_p, U32, U32, P(SecurityAttributes), U32, U32, V)
        self.InitializeProcThreadAttributeList = sig('InitializeProcThreadAttributeList', B, V, U32, U32, P(ctypes.c_size_t))
        self.UpdateProcThreadAttribute = sig('UpdateProcThreadAttribute', B, V, U32, ctypes.c_size_t, V, ctypes.c_size_t, V, V)
        self.DeleteProcThreadAttributeList = sig('DeleteProcThreadAttributeList', None, V)
        self.OpenProcess = sig('OpenProcess', V, U32, B, U32)
        self._k = k

    def err(self):
        return ctypes.get_last_error()

    def _raise_if_invalid(self, op):
        """Call IMMEDIATELY after a failed API call. Raises InvalidHandleError on
        ERROR_INVALID_HANDLE; otherwise returns the error code for the caller to classify."""
        e = self.err()
        if e == ERROR_INVALID_HANDLE:
            raise InvalidHandleError('%s: invalid handle.' % op)
        return e

    def create_job(self):
        """ALLOCATE ONLY (rev 9). Returns the raw job handle or raises with nothing allocated.
        No call that can fail runs after the allocation inside this method, so there is no
        window in which the adapter holds an identity the caller does not. Limits are applied
        by configure_job() AFTER the caller's owner has adopted the handle."""
        job = self.CreateJobObjectW(None, None)
        if not job:
            raise IsolationError('CreateJobObjectW failed (%d).' % self.err())
        return job

    def configure_job(self, job, limits):
        """SetInformationJobObject on a job the caller ALREADY owns. Never closes anything:
        the owner decides (6 -> InvalidHandleError -> owner quarantines; other -> IsolationError)."""
        if not self.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                            ctypes.byref(limits), ctypes.sizeof(limits)):
            self._raise_if_invalid('SetInformationJobObject')
            raise IsolationError('SetInformationJobObject failed (%d).' % self.err())

    def create_pipe(self):
        """Returns (read, write); both start inheritable."""
        sa = SecurityAttributes(ctypes.sizeof(SecurityAttributes), None, 1)
        r, w = ctypes.c_void_p(), ctypes.c_void_p()
        if not self.CreatePipe(ctypes.byref(r), ctypes.byref(w), ctypes.byref(sa), 0):
            raise IsolationError('CreatePipe failed (%d).' % self.err())
        return r.value, w.value

    def uninheritable(self, h):
        if not self.SetHandleInformation(h, HANDLE_FLAG_INHERIT, 0):
            self._raise_if_invalid('SetHandleInformation')
            raise IsolationError('SetHandleInformation failed (%d).' % self.err())

    def open_nul(self):
        sa = SecurityAttributes(ctypes.sizeof(SecurityAttributes), None, 1)
        h = self.CreateFileW('NUL', GENERIC_READ_WRITE_NUL, 3, ctypes.byref(sa), OPEN_EXISTING, 0, None)
        if h in (None, ctypes.c_void_p(-1).value):
            raise IsolationError('Open NUL failed (%d).' % self.err())
        return h

    def create_suspended(self, cmdline, env_block, cwd, h_in, h_out, h_err):
        # Only the three std handles are inherited (PROC_THREAD_ATTRIBUTE_HANDLE_LIST).
        handles = (ctypes.c_void_p * 3)(h_in, h_out, h_err)
        size = ctypes.c_size_t(0)
        self.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))  # sizing call; fails by design
        if size.value <= 0:                      # the sizing call must report a size
            raise IsolationError('InitializeProcThreadAttributeList reported no size (%d).' % self.err())
        buf = ctypes.create_string_buffer(size.value)
        if not self.InitializeProcThreadAttributeList(buf, 1, 0, ctypes.byref(size)):
            raise IsolationError('InitializeProcThreadAttributeList failed (%d).' % self.err())
        try:                                     # rev 9: from here the list is always deleted
            if not self.UpdateProcThreadAttribute(buf, 0, PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                                                  handles, ctypes.sizeof(handles), None, None):
                raise IsolationError('UpdateProcThreadAttribute failed (%d).' % self.err())
            si = StartupInfoExW()
            si.StartupInfo.cb = ctypes.sizeof(si)
            si.StartupInfo.dwFlags = STARTF_USESTDHANDLES
            si.StartupInfo.hStdInput, si.StartupInfo.hStdOutput, si.StartupInfo.hStdError = h_in, h_out, h_err
            si.lpAttributeList = ctypes.cast(buf, ctypes.c_void_p)
            pi = ProcessInformation()
            env = ctypes.create_string_buffer(env_block, len(env_block))
            # CreateProcessW may MODIFY lpCommandLine, so it must be a mutable buffer we own
            # (Microsoft Learn: not read-only memory). Never pass an immutable str.
            cmd_buf = make_command_line_buffer(cmdline)
            flags = CREATE_SUSPENDED | CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT | CREATE_NO_WINDOW
            # lpApplicationName given explicitly (python.exe path); no shell; no breakaway flag.
            ok = self.CreateProcessW(cmdline[0], ctypes.cast(cmd_buf, ctypes.c_void_p), None, None, True, flags,
                                     ctypes.cast(env, ctypes.c_void_p), cwd, ctypes.byref(si), ctypes.byref(pi))
            if not ok:
                raise IsolationError('CreateProcessW failed (%d).' % self.err())
            # rev 9: CreateProcessW succeeded; both handles exist. NOTHING that can raise may run
            # between here and the return (the finally below swallows its own failure).
            return pi.hProcess, pi.hThread
        finally:
            try: self.DeleteProcThreadAttributeList(buf)
            except Exception: pass               # list memory only; never masks/loses the two handles

    def assign(self, job, proc):
        if not self.AssignProcessToJobObject(job, proc):
            self._raise_if_invalid('AssignProcessToJobObject')   # job OR proc: caller quarantines both
            raise IsolationError('AssignProcessToJobObject failed (%d).' % self.err())

    def resume(self, thread):
        # ResumeThread returns the PREVIOUS suspend count: 0xFFFFFFFF = failure,
        # 1 = resumed from our single CREATE_SUSPENDED, >1 = still suspended,
        # 0 = was not suspended (it ran before assignment). Only 1 is acceptable.
        r = self.ResumeThread(thread)
        if r == 0xFFFFFFFF:
            self._raise_if_invalid('ResumeThread')
            raise IsolationError('ResumeThread failed (%d).' % self.err())
        if r != 1:
            raise IsolationError('ResumeThread returned unexpected suspend count %d.' % r)

    def wait(self, proc, ms):
        r = self.WaitForSingleObject(proc, ms)
        if r == WAIT_OBJECT_0: return True
        if r == WAIT_TIMEOUT: return False
        e = self.err()
        if e == ERROR_INVALID_HANDLE: raise InvalidHandleError('WaitForSingleObject: invalid handle.')
        raise IsolationError('WaitForSingleObject failed (result %#x, error %d).' % (r, e))

    def exit_code(self, proc):
        c = ctypes.c_uint32()
        if not self.GetExitCodeProcess(proc, ctypes.byref(c)):
            e = self.err()
            if e == ERROR_INVALID_HANDLE: raise InvalidHandleError('GetExitCodeProcess: invalid handle.')
            raise IsolationError('GetExitCodeProcess failed (%d).' % e)
        return c.value

    def terminate_job(self, job):
        """True only when TerminateJobObject reported success; callers must check.
        FALSE with ERROR_INVALID_HANDLE raises InvalidHandleError; other FALSE returns False."""
        if self.TerminateJobObject(job, 1):
            return True
        self._raise_if_invalid('TerminateJobObject')
        return False

    def terminate_process(self, proc):
        """Same contract as terminate_job."""
        if self.TerminateProcess(proc, 1):
            return True
        self._raise_if_invalid('TerminateProcess')
        return False

    def read(self, h, n):
        """Returns bytes, or None at EOF (write end closed). b'' is a successful empty read."""
        buf = ctypes.create_string_buffer(n); got = ctypes.c_uint32()
        if not self.ReadFile(h, buf, n, ctypes.byref(got), None):
            e = self.err()
            if e == ERROR_BROKEN_PIPE: return None      # the ONLY EOF signal
            if e == ERROR_INVALID_HANDLE: raise InvalidHandleError('ReadFile: invalid handle.')
            raise IsolationError('ReadFile failed (%d).' % e)
        return buf.raw[:got.value]      # b'' = success with no data: NOT EOF

    def write(self, h, data):
        """Returns bytes written (> 0). Failure or zero progress raises."""
        got = ctypes.c_uint32()
        if not self.WriteFile(h, data, len(data), ctypes.byref(got), None):
            e = self.err()
            if e == ERROR_INVALID_HANDLE: raise InvalidHandleError('WriteFile: invalid handle.')
            raise IsolationError('WriteFile failed (%d).' % e)
        if got.value == 0:
            raise IsolationError('WriteFile made no progress.')
        return got.value

    def cancel_io(self, h):
        """CancelIoEx(h, NULL). Documented scope: pending ASYNCHRONOUS I/O on h
        issued by this process (ReadFile docs). It is NOT the documented cancel
        for our synchronous pipe I/O; kept only as a fallback to
        cancel_sync_io."""
        if self.CancelIoEx(h, None):
            return True
        self._raise_if_invalid('CancelIoEx')      # 6 -> InvalidHandleError (owner quarantines)
        return False                              # 1168 ERROR_NOT_FOUND (nothing to cancel) and any other error: advisory

    def open_current_thread(self):
        """Real, owned handle to the CALLING thread (DuplicateHandle of the
        GetCurrentThread pseudo handle, THREAD_TERMINATE, not inheritable).
        Must be called ON the thread it names. No thread id is involved, so
        there is no id-reuse window; the handle pins the thread object."""
        out = ctypes.c_void_p()
        cur = self.GetCurrentProcess()
        if not self.DuplicateHandle(cur, self.GetCurrentThread(), cur, ctypes.byref(out),
                                    THREAD_TERMINATE, 0, 0) or not out.value:
            raise IsolationError('DuplicateHandle(current thread) failed (%d).' % self.err())
        return out.value

    def cancel_sync_io(self, hthread):
        """CancelSynchronousIo(hThread): the documented cancel for pending
        SYNCHRONOUS I/O issued by that thread. Returns False (ERROR_NOT_FOUND)
        when the thread is not inside an I/O request right now; the caller
        retries inside its join window. ERROR_INVALID_HANDLE raises InvalidHandleError;
        ERROR_NOT_FOUND (1168) is the ordinary False."""
        if self.CancelSynchronousIo(hthread):
            return True
        self._raise_if_invalid('CancelSynchronousIo')
        return False

    def open_process(self, pid, access=PROCESS_QUERY_LIMITED_INFORMATION):
        return self.OpenProcess(access, 0, pid)

    def close(self, h):
        """Returns True when the handle was closed (or there was none)."""
        if not h: return True
        return bool(self.CloseHandle(h))


class _Handle:
    """Single-owner handle with an identity quarantine (rev 7).

    States: OPEN (value usable) -> CLOSED (value erased, only after the OS reported a
    successful CloseHandle) or QUARANTINED (CloseHandle returned FALSE/raised, or an
    operation reported ERROR_INVALID_HANDLE). A quarantined value is kept ONLY so that it
    still counts as open (capacity stays reserved, the run fails closed). It is never passed
    to any API again: value() returns None, close() returns False without calling, cancel()
    does nothing. The lock serializes state changes; it does NOT prove kernel object identity
    and nothing here relies on it for that."""
    def __init__(self, api, h, name='handle'):
        self.api, self.h, self.name = api, h, name
        self.lock = threading.Lock(); self.quarantined = False; self.quarantine_reason = None

    def adopt(self, h):
        """Rev 9: owners are created BEFORE the allocation (h=None) and adopt the raw value
        in one step, so there is no window with an allocated, unowned handle."""
        with self.lock:
            if self.h:
                raise IsolationError('owner %s already holds a handle' % self.name)
            self.h = h or None
        return self

    @property
    def is_open(self):
        return bool(self.h)

    def value(self):
        """The numeric identity for an API call, or None when closed or quarantined."""
        with self.lock:
            return self.h if (self.h and not self.quarantined) else None

    def poison(self, reason):
        with self.lock:
            if self.h and not self.quarantined:
                self.quarantined = True; self.quarantine_reason = reason

    def close(self):
        """True when closed (or nothing to close). One real CloseHandle call at most, ever."""
        with self.lock:
            if not self.h:
                return True
            if self.quarantined:
                return False
            try: ok = bool(self.api.close(self.h))
            except Exception: ok = False
            if ok: self.h = None
            else:
                self.quarantined = True; self.quarantine_reason = 'CloseHandle failed'
            return ok

    def cancel(self):
        with self.lock:
            if self.h and not self.quarantined:
                try: self.api.cancel_io(self.h)     # fallback only; advisory by design
                except InvalidHandleError: self.quarantined = True; self.quarantine_reason = 'invalid handle'
                except Exception: pass


class _ThreadSlot:
    """Parent-owned slot for the REAL handle an I/O thread duplicates to itself.

    Same quarantine rule as _Handle: one CloseHandle call at most; a failed close or an
    invalid-handle report quarantines the value (counts as open, never used again)."""
    def __init__(self):
        self.lock = threading.Lock(); self.h = None; self.sealed = False
        self.quarantined = False; self.quarantine_reason = None

    def publish(self, api):            # runs ON the I/O thread
        h = api.open_current_thread()
        with self.lock:
            if not self.sealed:
                self.h = h
                return
        ok = False
        try: ok = bool(api.close(h))   # parent already gave up on this slot
        except Exception: pass
        if not ok:                     # never forget it, never use it again
            with self.lock:
                self.h = h; self.quarantined = True; self.quarantine_reason = 'CloseHandle failed'

    def cancel(self, api):             # parent; None when no usable handle
        with self.lock:
            if not self.h or self.quarantined:
                return None
            try: return bool(api.cancel_sync_io(self.h))
            except InvalidHandleError:
                self.quarantined = True; self.quarantine_reason = 'invalid handle'; return False
            except Exception: return False

    def close(self, api):              # parent; ONLY after the thread ended
        with self.lock:
            self.sealed = True
            if not self.h:
                return True
            if self.quarantined:
                return False
            try: ok = bool(api.close(self.h))
            except Exception: ok = False
            if ok: self.h = None
            else:
                self.quarantined = True; self.quarantine_reason = 'CloseHandle failed'
            return ok

    @property
    def is_open(self):
        return bool(self.h)


def _wait_exit(api, proc, seconds, owner=None):
    """Bounded wait for process exit. Returns True ONLY on a confirmed exit.
    Timeout, a raised error, or an exception all return False: the result is
    then unknown/failure, never proof of death."""
    if owner is not None:
        proc = owner.value()
        if proc is None: return False        # quarantined or closed: never waited on again
    try:
        return bool(api.wait(proc, int(seconds * 1000)))
    except InvalidHandleError:
        if owner is not None: owner.poison('invalid handle')
        return False
    except Exception:
        return False


def handle_state(h, api):
    """Tri-state liveness for a process HANDLE we own or retained: 'alive' when
    GetExitCodeProcess reports STILL_ACTIVE, 'dead' when it reports a finished
    exit code, 'unknown' when the query itself fails. A finished code read from
    a real handle is the only established proof of death we accept."""
    code = ctypes.c_uint32()
    if not api.GetExitCodeProcess(h, ctypes.byref(code)):
        return PID_UNKNOWN
    return PID_ALIVE if code.value == STILL_ACTIVE else PID_DEAD


def pid_state(pid, api=None):
    """Tri-state liveness for an arbitrary process id: 'alive' / 'dead' / 'unknown'.
    TEST/DIAGNOSTIC HELPER ONLY: run_in_job never calls it.

    The OpenProcess documentation establishes only two failure meanings:
    pid 0 fails with ERROR_INVALID_PARAMETER (the idle process EXISTS) and the
    System/CSRSS processes fail with ERROR_ACCESS_DENIED (they EXIST). No error
    is documented to mean 'no such pid' for an arbitrary positive pid, so EVERY
    OpenProcess failure is 'unknown', never 'dead'.
    IDENTITY CAUTION (rev 5): a pid is reusable. A SUCCESSFUL open by pid may
    name a different, newer process, so even 'alive'/'dead' here describes
    "some process that has this pid now", not the process we once knew. The only
    identity-safe liveness is handle_state() on a handle retained from before
    the event (the orphan test does this while the target is provably alive).
    Windows-only helper; on other platforms it returns 'unknown'.
    """
    if api is None:
        if sys.platform != 'win32':
            return PID_UNKNOWN
        api = WinApi()
    h = api.open_process(pid)
    if not h:
        return PID_UNKNOWN
    owner = _Handle(api, h, 'diagnostic-process')
    try:
        v = owner.value()
        return handle_state(v, api) if v is not None else PID_UNKNOWN
    finally:
        if not owner.close():            # rev 9: result checked; a failed close is quarantined and counted
            _retain_diagnostic(api, owner)


def _last_error(api):
    try: return api.err()
    except Exception: return '?'


def _terminate_process(api, proc, owner=None):
    """TerminateProcess, result classified. Returns (ok, err). An invalid-handle report
    (raised by the adapter, or err 6 from a legacy adapter) quarantines the owner."""
    try:
        ok = bool(api.terminate_process(proc)); err = None if ok else _last_error(api)
    except InvalidHandleError:
        ok, err = False, ERROR_INVALID_HANDLE
    except Exception:
        ok, err = False, '?'
    if not ok and err == ERROR_INVALID_HANDLE and owner is not None:
        owner.poison('invalid handle')
    return ok, err


def _terminate_tree(api, job, proc, hjob=None, hproc=None):
    """TerminateJobObject with its result CHECKED. Returns (ok, error).
    ok is True only when the OS reported success. On failure the main process
    is also asked to terminate (best effort, result not trusted); the caller
    must still treat the job tree as NOT proven terminated."""
    ok, err = False, None
    if hjob is not None: job = hjob.value()      # quarantined/closed identity -> None -> not used
    if hproc is not None: proc = hproc.value()
    if job is not None:
        try: ok = bool(api.terminate_job(job))
        except InvalidHandleError: ok, err = False, ERROR_INVALID_HANDLE
        except Exception: ok = False
        if not ok:
            if err is None: err = _last_error(api)
            if err == ERROR_INVALID_HANDLE and hjob is not None: hjob.poison('invalid handle')
    if not ok and proc is not None:
        _terminate_process(api, proc, hproc)
    return ok, err


def _cancel_blocked_threads(api, threads, slots):
    """Documented cancel for our SYNCHRONOUS pipe I/O: CancelSynchronousIo on the
    handle each thread duplicated to itself. Only live threads are targeted.
    Results are advisory by design (ERROR_NOT_FOUND is normal while the thread is
    between I/O calls); the real outcome is whether the thread ends."""
    for t, slot in zip(threads, slots):
        if t.is_alive():
            slot.cancel(api)


def _stop_io_threads(threads, pipe_ends, api=None, slots=()):
    """Explicit ownership: bounded join, then cancel-and-retry inside ONE bounded
    join window. Returns True only when every thread ended. Callers invoke this
    at most once per run (the ceiling arithmetic depends on it)."""
    join_deadline = time.monotonic() + JOIN_WAIT_SECONDS
    for t in threads:
        t.join(max(0.0, min(CANCEL_AFTER_SECONDS, join_deadline - time.monotonic())))
    while any(t.is_alive() for t in threads):
        if join_deadline - time.monotonic() <= 0:
            break
        if api is not None and slots:
            _cancel_blocked_threads(api, threads, slots)
        for h in pipe_ends:
            if h is not None:
                h.cancel()
        for t in threads:
            t.join(max(0.0, min(POLL_SECONDS, join_deadline - time.monotonic())))
    return not any(t.is_alive() for t in threads)


# ---- retained owner (rev 5 design change) -----------------------------------
class _Retained:
    """Handles we may NOT close yet (or failed to close). Owned by the module until reaped.
    controls: _Handle objects (thread, process, job) or None. assigned: True only when
    AssignProcessToJobObject succeeded; an unassigned child is contained ONLY by direct
    TerminateProcess. lock: exclusive per-entry reap right (non-blocking acquire)."""
    def __init__(self, reason, api, controls, assigned, exit_ok, term_ok,
                 io_handles, threads, slots):
        self.reason, self.api = reason, api
        self.thread, self.proc, self.job = controls
        self.assigned = bool(assigned)
        self.exit_ok, self.term_ok = bool(exit_ok), bool(term_ok)
        self.io_handles, self.threads, self.slots = list(io_handles), list(threads), list(slots)
        self.lock = threading.Lock(); self.done = False

    @property
    def control_open(self):
        return any(h is not None and h.is_open for h in (self.thread, self.proc, self.job))

    @property
    def io_open(self):
        return any(h.is_open for h in self.io_handles) or any(s.is_open for s in self.slots)

    @property
    def quarantined(self):
        """Names of handles whose identity is quarantined (never used again, never freed)."""
        names = [h.name for h in (self.thread, self.proc, self.job, *self.io_handles) if h is not None and h.quarantined]
        return names + ['io-thread-handle' for sl in self.slots if sl.quarantined]


_RETAINED = []
_RETAINED_LOCK = threading.Lock()
_ACTIVE = [0]                   # in-flight runs holding an admission reservation (under _RETAINED_LOCK)


def retained_count():
    with _RETAINED_LOCK:
        return len(_RETAINED)


def active_count():
    with _RETAINED_LOCK:
        return _ACTIVE[0]


def _admit():
    """Atomic admission: retained + in-flight must stay below MAX_RETAINED. Returns a token."""
    with _RETAINED_LOCK:
        n = len(_RETAINED) + _ACTIVE[0]
        if n >= MAX_RETAINED:
            return None, n
        _ACTIVE[0] += 1
        return {'held': True}, n


def _retain_diagnostic(api, owner):
    """A diagnostic-only handle whose CloseHandle failed: kept (quarantined, never reused) and
    counted against capacity like any other unclosed identity."""
    rec = _Retained('diagnostic handle: CloseHandle failed', api, (None, owner, None), False, True, True, [], [], [])
    with _RETAINED_LOCK:
        _RETAINED.append(rec)


def _release_locked(token):          # caller holds _RETAINED_LOCK
    if token is not None and token['held']:
        token['held'] = False
        _ACTIVE[0] -= 1


def _release(token):
    with _RETAINED_LOCK:
        _release_locked(token)


def _close_controls(api, e):
    """Close thread, process, job (job LAST). A failed close KEEPS that handle in the entry."""
    for h in (e.thread, e.proc, e.job):
        if h is not None:
            h.close()


def _reap_one(api, e):
    """Finish one retained entry. Caller holds e.lock (exclusive).
    Rev 7: every identity is read through _Handle.value(), so a quarantined process or job
    value is never waited on, terminated or closed again. A quarantined process means exit
    can never be confirmed again: the other control handles then stay open (fail closed).
    Returns True when nothing remains open."""
    pr = e.proc.value() if e.proc is not None else None
    jb = e.job.value() if e.job is not None else None
    if e.control_open:
        if e.assigned:
            if not e.term_ok and jb is not None:
                e.term_ok = _terminate_tree(api, jb, pr, e.job, e.proc)[0]
        elif not e.exit_ok and pr is not None:
            _terminate_process(api, pr, e.proc)        # unassigned child: ONLY direct kill contains it; retry
        if not e.exit_ok and pr is not None and e.proc.value() is not None:
            e.exit_ok = _wait_exit(api, pr, REAP_WAIT_SECONDS, e.proc)
        # proc handle absent (never created) is the ONLY case where exit need not be confirmed
        if (e.term_ok or not e.assigned) and (e.exit_ok or e.proc is None):
            _close_controls(api, e)
    if e.io_open:
        if any(t.is_alive() for t in e.threads):
            _cancel_blocked_threads(api, e.threads, e.slots)
        if not any(t.is_alive() for t in e.threads):
            for sl in e.slots: sl.close(api)
            for h in e.io_handles: h.close()
    return not (e.control_open or e.io_open)


def reap_retained():
    """Retry cleanup of retained children. Returns how many remain. Each entry is reaped by
    at most ONE caller at a time (non-blocking per-entry lock); an entry another reaper holds
    counts as remaining. Makes no API call when nothing is retained."""
    with _RETAINED_LOCK:
        entries = list(_RETAINED)
    for e in entries:
        if not e.lock.acquire(False):
            continue
        try:
            if e.done:
                continue
            if _reap_one(e.api, e):
                e.done = True
                with _RETAINED_LOCK:
                    if e in _RETAINED: _RETAINED.remove(e)
        finally:
            e.lock.release()
    return retained_count()


def run_in_job(cmdline, stdin_bytes, *, env, cwd, api=None, wall=WALL_SECONDS,
               cpu_seconds=CPU_SECONDS, memory_bytes=MEMORY_BYTES, output_limit=OUTPUT_BYTES,
               active_process_limit=1):
    """Admission wrapper (rev 6). The cap is RESERVED atomically: retained + in-flight runs
    never exceed MAX_RETAINED, so concurrent callers cannot overshoot it. (Consequence:
    at most MAX_RETAINED parser runs may be in flight at once.) See _run_in_job."""
    if api is None:
        api = WinApi()
    if not isinstance(stdin_bytes, bytes) or len(stdin_bytes) > INPUT_BYTES:
        raise IsolationError('Bounded parser input required.')
    block = environment_block(env)
    reap_retained()
    token, n = _admit()
    if token is None:
        raise IsolationError('Too many unreaped isolated children (%d); refusing to start another.' % n)
    try:
        return _run_in_job(cmdline, stdin_bytes, block, token, env=env, cwd=cwd, api=api, wall=wall,
                           cpu_seconds=cpu_seconds, memory_bytes=memory_bytes,
                           output_limit=output_limit, active_process_limit=active_process_limit)
    finally:
        _release(token)


def _run_in_job(cmdline, stdin_bytes, block, token, *, env, cwd, api, wall,
                cpu_seconds, memory_bytes, output_limit, active_process_limit):
    """Run cmdline in a limited Job Object; return stdout bytes (<= output_limit).

    Order is fixed: job+limits -> pipes -> CREATE_SUSPENDED -> assign -> resume.
    The wall clock starts after the pre-run reap (setup counts). See TIME BUDGET above.
    Success requires ALL of: all stdin delivered, stdout read to EOF, process exited itself
    with code 0 within the budget, TerminateJobObject reported success, output within
    limit, every handle closed successfully. Everything else raises IsolationError.

    HANDLE RULE (rev 5): a handle that controls the child (process, primary thread, job)
    is closed only after exit is confirmed AND the job kill succeeded; otherwise it is
    RETAINED (see _Retained) and the error says so. Never closed under a running thread.

    active_process_limit=0 lifts the per-job active-process cap; reserved for the
    orphan-kill acceptance test, never for production parser runs.
    """
    start = time.monotonic()
    deadline = start + wall
    job = proc = thread = None
    hj = hp = ht = None        # owning _Handle objects for job / process / primary thread
    assigned = False           # True only after AssignProcessToJobObject succeeded
    unassigned = False         # child exists but assign failed: contained ONLY by direct TerminateProcess
    child_ends = []            # in_r, out_w, nul: closed right after CreateProcess (copies only)
    in_w = out_r = None        # _Handle objects: owned by writer / reader thread once started
    st = {'close_err': [], 'overflow': False, 'out': bytearray(), 'w_done': False, 'w_err': None,
          'r_eof': False, 'r_err': None}
    slot_w, slot_r = _ThreadSlot(), _ThreadSlot()
    started = []               # (thread, slot) appended ONLY after Thread.start() succeeded
    exited = False
    exit_confirmed = False     # death established by a bounded wait, natural or killed
    exit_checked = False       # the ONE confirmation wait has been spent
    term_ok = None             # None = not attempted; True/False = TerminateJobObject result
    term_err = None
    io_stopped = None          # None = stop window not spent; else its result
    try:
        # REV 9 OWNERSHIP RULE: every owner exists (empty) BEFORE the allocating call, and adopts the
        # raw value the instant it returns. An allocator never returns a handle that has to be closed
        # by the adapter: adapters ALLOCATE ONLY, and every later failure is handled by the owner.
        hj = _Handle(api, None, 'job')
        in_w = _Handle(api, None, 'stdin-parent-end'); out_r = _Handle(api, None, 'stdout-parent-end')
        c_in, c_out, c_nul = (_Handle(api, None, 'stdin-child-end'), _Handle(api, None, 'stdout-child-end'),
                              _Handle(api, None, 'nul'))
        child_ends.extend((c_in, c_out, c_nul))
        hp = _Handle(api, None, 'process'); ht = _Handle(api, None, 'primary-thread')
        hj.adopt(api.create_job()); job = hj.value()
        try:
            api.configure_job(job, build_limits(cpu_seconds, memory_bytes, active_process_limit))
        except InvalidHandleError:
            hj.poison('invalid handle'); raise      # quarantined: counted, never closed/used again
        r_, w_ = api.create_pipe(); c_in.adopt(r_); in_w.adopt(w_)
        r_, w_ = api.create_pipe(); out_r.adopt(r_); c_out.adopt(w_)
        c_nul.adopt(api.open_nul())
        try:
            api.uninheritable(in_w.value())
        except InvalidHandleError:
            in_w.poison('invalid handle'); raise
        try:
            api.uninheritable(out_r.value())      # parent ends must not leak to child
        except InvalidHandleError:
            out_r.poison('invalid handle'); raise
        proc, thread = api.create_suspended(cmdline, block, cwd, c_in.value(), c_out.value(), c_nul.value())
        hp.adopt(proc); ht.adopt(thread)
        try:
            api.assign(job, proc)           # child has executed zero instructions so far
            assigned = True
        except BaseException as assign_exc:
            unassigned = True
            # The child is NOT in the job (assign failed), so the job holds nothing to kill.
            if isinstance(assign_exc, InvalidHandleError):
                # Invalid handle on a call naming job AND process: which one is unknowable.
                # Quarantine both; neither number is used again (no terminate, no wait).
                hj.poison('invalid handle'); hp.poison('invalid handle')
                tp_ok, tp_err = False, ERROR_INVALID_HANDLE
            else:
                tp_ok, tp_err = _terminate_process(api, proc, hp)
            term_ok = None                  # rev 6: NOT True. The empty job contains nothing; only exit does.
            exit_checked = True
            # TerminateProcess is asynchronous: exit MUST be confirmed before any
            # handle it owns is closed. Unconfirmed = failure/unknown, never death.
            if _wait_exit(api, proc, KILL_WAIT_SECONDS, hp):
                exit_confirmed = True; term_ok = True
                if not tp_ok:
                    raise IsolationError('Assignment failed; TerminateProcess reported failure (%s) '
                                         'but exit was confirmed.' % tp_err) from assign_exc
            else:
                extra = '' if tp_ok else ' TerminateProcess also failed (%s).' % tp_err
                raise IsolationError('Assignment failed and child exit could not be confirmed.%s' % extra) from assign_exc
            raise
        if time.monotonic() >= deadline:
            raise IsolationError('Fingerprint parser safety budget exceeded.')   # never resumed
        for c in child_ends:
            if not c.close(): raise IsolationError('CloseHandle failed on a child-side pipe end.')   # c stays owned: cleanup retains it
        child_ends = []
        try:
            api.resume(thread)              # WinApi.resume requires previous suspend count == 1
        except InvalidHandleError:
            ht.poison('invalid handle'); raise

        def feed():
            try:
                slot_w.publish(api)         # FIRST: real handle to ourselves, before any I/O
                view = memoryview(stdin_bytes); i = 0
                while i < len(view):
                    wv = in_w.value()
                    if wv is None: raise InvalidHandleError('stdin parent end quarantined')
                    n = api.write(wv, bytes(view[i:i + 65536]))
                    if n <= 0:
                        raise IsolationError('WriteFile made no progress.')   # never spin on zero progress
                    i += n
                st['w_done'] = True
            except BaseException as e:
                st['w_err'] = e
                if isinstance(e, InvalidHandleError): in_w.poison('invalid handle')
            finally:
                if not in_w.close(): st['close_err'].append('stdin-parent-end')

        def drain():
            try:
                slot_r.publish(api)         # FIRST: real handle to ourselves, before any I/O
                zero = 0
                while True:
                    rv = out_r.value()
                    if rv is None: raise InvalidHandleError('stdout parent end quarantined')
                    chunk = api.read(rv, 65536)
                    if chunk is None:       # ERROR_BROKEN_PIPE: the only EOF
                        st['r_eof'] = True; return
                    if not chunk:           # successful empty read: NOT EOF
                        zero += 1
                        if zero > MAX_ZERO_READS:
                            raise IsolationError('ReadFile returned no data repeatedly without EOF.')
                        continue
                    zero = 0
                    st['out'] += chunk
                    if len(st['out']) > output_limit:
                        st['overflow'] = True; return
            except BaseException as e:
                st['r_err'] = e
                if isinstance(e, InvalidHandleError): out_r.poison('invalid handle')
            finally:
                if not out_r.close(): st['close_err'].append('stdout-parent-end')

        # Named, non-daemon threads owned by THIS function. A thread is tracked only
        # once start() succeeded, so cleanup never joins a never-started thread.
        for target, name, slot in ((feed, 'job-stdin-writer', slot_w), (drain, 'job-stdout-reader', slot_r)):
            t = threading.Thread(target=target, daemon=False, name=name)
            try:
                t.start()
            except BaseException as e:
                raise IsolationError('Could not start I/O thread %s.' % name) from e
            started.append((t, slot))
        io_threads = lambda: tuple(t for t, _ in started)
        io_slots = lambda: tuple(s for _, s in started)
        io_failed = False
        while True:
            if st['overflow'] or st['w_err'] or st['r_err']:
                io_failed = True; break
            left = deadline - time.monotonic()
            if left <= 0: break
            try:
                pv = hp.value()
                if pv is None: raise IsolationError('process identity quarantined')
                gone = api.wait(pv, max(1, int(min(left, POLL_SECONDS) * 1000)))
            except InvalidHandleError:
                hp.poison('invalid handle')
                raise IsolationError('Process state could not be confirmed.')
            except Exception:
                raise IsolationError('Process state could not be confirmed.')   # unknown, never assumed dead
            if gone:
                exited = True; exit_confirmed = True; exit_checked = True; break
        # Terminate the whole job ONCE on EVERY path - normal exit included - BEFORE
        # the I/O threads are joined, and CHECK the result (rev 5): a failed
        # TerminateJobObject means descendants are not proven dead, so the run fails
        # and the handles are retained, even when the main process exited normally.
        term_ok, term_err = _terminate_tree(api, job, proc, hj, hp)
        # A confirmed kill is NOT a success: `exited` stays False, the run failed.
        if not exited:
            exit_checked = True
            if not _wait_exit(api, proc, KILL_WAIT_SECONDS, hp):
                raise IsolationError('Killed process exit could not be confirmed.')
            exit_confirmed = True
        io_stopped = _stop_io_threads(io_threads(), (in_w, out_r), api, io_slots())
        if not io_stopped:
            raise IsolationError('Fingerprint parser I/O did not stop; handles left open.')
        if st['close_err']:
            raise IsolationError('CloseHandle failed on %s; identity kept, success blocked.' % ', '.join(st['close_err']))
        if not term_ok:
            raise IsolationError('TerminateJobObject failed (error %s): job tree not proven terminated.' % term_err)
        if st['overflow']: raise IsolationError('Fingerprint parser result exceeds safety budget.')
        if io_failed and st['w_err']: raise IsolationError('Fingerprint parser input was not fully delivered.')
        if io_failed and st['r_err']: raise IsolationError('Fingerprint parser output was incomplete.')
        if not exited: raise IsolationError('Fingerprint parser safety budget exceeded.')
        if st['w_err'] or not st['w_done']: raise IsolationError('Fingerprint parser input was not fully delivered.')
        if st['r_err'] or not st['r_eof']: raise IsolationError('Fingerprint parser output was incomplete.')
        try:
            pv = hp.value()
            code = api.exit_code(pv) if pv is not None else None
        except InvalidHandleError:
            hp.poison('invalid handle'); code = None
        if code is None: raise IsolationError('Fingerprint parser exit code could not be read.')
        if code != 0: raise IsolationError('Fingerprint parser refused or exceeded safety budget.')
        return bytes(st['out'])
    finally:
        # ---- cleanup: each phase at most once; handles close only when safe ----
        pending = sys.exc_info()[1]
        notes = []
        leftover_io = []; close_failed = []; threads = (); retained_done = False
        try:
            # (1) child-side pipe copies: closing them never affects the child. A copy whose close
            #     FAILS stays owned (moved into the retained entry), never dropped.
            leftover_io = [c for c in child_ends if not c.close()]
            # (2) a child exists but was never killed on this path: kill it (result checked).
            if proc is not None and term_ok is None and not unassigned:
                term_ok, term_err = _terminate_tree(api, job, proc, hj, hp)
            if proc is not None and not exit_checked:
                exit_checked = True
                exit_confirmed = _wait_exit(api, proc, KILL_WAIT_SECONDS, hp)
            if proc is None:
                term_ok = True          # no process ever existed in the job
            if unassigned:
                term_ok = bool(exit_confirmed)   # unassigned: containment == confirmed exit, nothing else
            # (3) I/O threads: one stop window, only if not spent already.
            threads = tuple(t for t, _ in started); slots = tuple(s for _, s in started)
            if threads and io_stopped is None:
                io_stopped = _stop_io_threads(threads, (in_w, out_r), api, slots)
            threads_alive = any(t.is_alive() for t in threads)
            control_safe = (proc is None or exit_confirmed) and bool(term_ok)
            io_safe = not threads_alive
            close_failed = [c.name for c in leftover_io]
            # (4) I/O handles: only after their threads ended. Anything not closed stays owned.
            slots_left = []
            for t, s in started:
                if t.is_alive():
                    slots_left.append(s)                  # live thread keeps its own slot
                elif not s.close(api):
                    slots_left.append(s); close_failed.append('io-thread-handle')
            for h in (in_w, out_r):
                if h is None: continue
                if io_safe and not h.close():
                    close_failed.append(h.name)
                if h.is_open: leftover_io.append(h)
            # (5) controls: only after confirmed exit AND containment. Job LAST.
            ctrl = [h for h in (ht, hp, hj) if h is not None]
            if control_safe:
                for h in ctrl:
                    if not h.close(): close_failed.append(h.name)
            if any(h.is_open for h in ctrl) or leftover_io or slots_left:
                reason = []
                if proc is not None and not exit_confirmed: reason.append('child exit not confirmed')
                if proc is not None and not term_ok and not unassigned: reason.append('job termination not confirmed')
                if unassigned and not exit_confirmed: reason.append('unassigned child: direct termination not confirmed')
                if threads_alive: reason.append('I/O thread still running')
                if close_failed: reason.append('CloseHandle failed')
                rec = _Retained('; '.join(reason) or 'unspecified', api, (ht, hp, hj), assigned,
                                exit_confirmed or proc is None, bool(term_ok),
                                leftover_io, [t for t, _ in started if t.is_alive()], slots_left)
                with _RETAINED_LOCK:
                    _RETAINED.append(rec)
                    retained_done = True
                    _release_locked(token)               # reservation becomes the retained entry atomically
                if proc is not None and not (exit_confirmed and term_ok):
                    notes.append('retained, NOT closed (%s); child state unknown, not proof of death' % rec.reason)
                else:
                    notes.append('retained after failure (%s); identity kept for retry' % rec.reason)
        except BaseException as cleanup_exc:
            # REV 9: cleanup itself failed unexpectedly. NEVER drop an owner: transfer everything
            # still open to the retained owner (capacity reservation converts atomically), with
            # exit/termination marked UNCONFIRMED so the reaper re-kills and re-waits before any close.
            seen = set(); keep_io = []
            for h in (in_w, out_r, *child_ends, *leftover_io):
                if h is not None and h.is_open and id(h) not in seen:
                    seen.add(id(h)); keep_io.append(h)
            slots_open = [sl for _, sl in started if sl.is_open]
            if not retained_done and (keep_io or slots_open or any(h is not None and h.is_open for h in (ht, hp, hj))):
                rec = _Retained('cleanup raised %s; everything still open retained, nothing assumed dead'
                                % type(cleanup_exc).__name__, api, (ht, hp, hj), assigned, False, False,
                                keep_io, [t for t, _ in started if t.is_alive()], slots_open)
                with _RETAINED_LOCK:
                    _RETAINED.append(rec)
                    _release_locked(token)
            raise
        if close_failed:
            notes.append('CloseHandle failed for: %s' % ', '.join(close_failed))
        if notes:
            if isinstance(pending, IsolationError):
                raise IsolationError('%s [%s]' % (pending, '; '.join(notes))) from pending
            if pending is None:
                raise IsolationError('Cleanup: %s.' % '; '.join(notes))


def self_check_in_expected_job():
    """Run INSIDE the worker on Windows before parsing. Fail closed.

    QueryInformationJobObject(NULL, JobObjectExtendedLimitInformation) reads the
    limits of the job this process is in (documented: NULL handle = caller's job).
    """
    if sys.platform != 'win32':
        raise RuntimeError('Not Windows.')
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    info = JobExtendedLimit(); ret = ctypes.c_uint32()
    k.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    k.QueryInformationJobObject.restype = ctypes.c_int32
    if not k.QueryInformationJobObject(None, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(ret)):
        raise RuntimeError('Worker is not in a job object.')
    b = info.BasicLimitInformation
    if (b.LimitFlags & REQUIRED_FLAGS) != REQUIRED_FLAGS or not 0 < b.PerProcessUserTimeLimit <= CPU_SECONDS * TICKS_PER_SECOND \
            or not 0 < info.ProcessMemoryLimit <= MEMORY_BYTES or b.ActiveProcessLimit != 1:
        raise RuntimeError('Worker job limits are not the required limits.')
