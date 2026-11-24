"""REV 6 NOTE: the "Rev 6" section sits just before the Windows-only group; each rev 6 test fails on the rev 5 module (checked by execution).
REV 5 NOTE: see the "Rev 5" section at the bottom; every rev 5 test fails on the rev 4
module (checked by execution). The two rev 4 emergency-close tests were REPLACED
(design change: handles are retained, never closed, while exit is unconfirmed).

Two groups, kept separate on purpose.

A. PORTABLE / FAKE-API tests: run anywhere (Linux CI ok). They prove only our
   orchestration order, bounds, cleanup protocol and layout arithmetic against a
   deterministic fake. They prove NOTHING about what Windows enforces.
   Rev 3: one deterministic fake-API test per failure path (reap timeout and
   reap error on assignment failure, unconfirmed kill at run stage, PID helper
   tri-state including access-denied vs not-found, thread ownership/ordering).
   Rev 4 (writer-review reconciliation): tests for the synchronous-cancel path
   (CancelSynchronousIo, not CancelIoEx, for our synchronous pipe I/O), job
   termination on a normal exit so a descendant holding stdout cannot stall the
   reader, the emergency-close protocol after an unconfirmed exit (backstop
   fires before the process handle closes, confirmation re-attempted, fallback
   labelled), and OpenProcess failures reading 'unknown' for ANY error. Each
   new test FAILS against the rev 3 module.
B. WINDOWS-ONLY tests (marked windows_only): skipped off Windows. UNRUN as of
   authoring. These are the acceptance evidence and must pass on a real device.
"""
import ctypes, subprocess, sys, threading, time
import pytest
import app.windows_job_isolation as w


@pytest.fixture(autouse=True)
def _clean_retained():
    if hasattr(w, '_RETAINED'): w._RETAINED.clear()
    yield
    if hasattr(w, '_RETAINED'): w._RETAINED.clear()

windows_only = pytest.mark.skipif(sys.platform != 'win32', reason='needs real Windows; unrun at authoring')


class Fake:
    """behavior: ok | echo | hang | flood | stuck_reader | stuck_forever | descendant.
    writer: ok | error | zero | early_exit_error.
    wait_error: if True, api.wait raises (cleanup must treat as unknown, not death).
    cancel_sync_fails: if True, OpenThread fails (CancelIoEx fallback must cover).
    confirm_on_job_close: if True, api.wait confirms exit only after the job
    handle was closed (models the KILL_ON_JOB_CLOSE backstop kill).
    'descendant': main process exits normally (wait confirms) but the reader
    sees no EOF until terminate_job kills the descendant holding the pipe."""
    def __init__(self, assign_fails=False, behavior='ok', exit_code=0, payload=b'{}', writer='ok',
                 wait_error=False, never_exits=False, cancel_sync_fails=False,
                 confirm_on_job_close=False, terminate_job_ok=True,
                 terminate_process_ok=True, close_fail=()):
        self.terminate_job_ok = terminate_job_ok; self.terminate_process_ok = terminate_process_ok
        self.close_fail = set(close_fail); self.thread_handles = {}; self.unrelated_cancels = []
        self.by_id_opens = 0; self.last_error = 0
        self.calls = []; self.assign_fails = assign_fails; self.behavior = behavior
        self.exit_code_v = exit_code; self.payload = payload; self.limits = None
        self.closed = []; self.n = 0; self.sent = bytearray(); self.writer = writer
        self.killed = False; self.release = threading.Event(); self.out_r = None
        self.wait_error = wait_error; self.never_exits = never_exits
        self.cancel_sync_fails = cancel_sync_fails
        self.confirm_on_job_close = confirm_on_job_close
        self.job_handle = None; self.job_closed = False
        self.opened_threads = []; self.sync_cancels = []
    def _h(self): self.n += 1; return 100 + self.n
    def _log(self, c): self.calls.append(c)
    def create_job(self):
        self._log('job'); self.job_handle = self._h(); return self.job_handle
    def configure_job(self, job, limits): self.limits = limits
    def create_pipe(self):
        self._log('pipe'); r, wr = self._h(), self._h()
        if self.out_r is None and len(self.calls) and self.calls.count('pipe') == 2: self.out_r = r
        return r, wr
    def uninheritable(self, h): self._log('uninherit')
    def open_nul(self): self._log('nul'); return self._h()
    def create_suspended(self, cmd, block, cwd, a, b, c):
        self._log('create_suspended'); self.block = block; self.proc = self._h(); self.thr = self._h()
        return self.proc, self.thr
    def assign(self, j, p):
        self._log('assign')
        if self.assign_fails: raise w.IsolationError('assign failed')
    def resume(self, t): self._log('resume')
    def wait(self, p, ms):
        self._log('wait')
        if self.wait_error: raise w.IsolationError('WaitForSingleObject failed.')
        if self.confirm_on_job_close: return self.job_closed      # dies only via the job-close backstop
        if self.never_exits: time.sleep(ms / 1000); return False   # never confirms, even after a kill
        if self.behavior in ('hang', 'stuck_reader', 'stuck_forever') and not self.killed:
            time.sleep(ms / 1000); return False
        return True
    def exit_code(self, p): return self.exit_code_v
    def err(self): return 5
    def terminate_job(self, j):
        self._log('terminate_job')
        if not self.terminate_job_ok: return False
        self.killed = True
        if self.behavior == 'descendant': self.release.set()       # descendant dies -> pipe EOFs
        return True
    def terminate_process(self, p):
        self._log('terminate_process')
        if not self.terminate_process_ok: return False
        self.killed = True; return True
    def cancel_io(self, h):
        self._log('cancel_io:%d' % h)
        if self.behavior == 'stuck_reader': self.release.set()
        return True
    def open_current_thread(self):
        # runs ON the I/O thread; records which thread the handle names
        self._log('open_current_thread')
        h = 7000 + len(self.thread_handles); self.thread_handles[h] = threading.current_thread().name; return h
    def open_thread(self, tid):
        # rev 4 API (by native id): models an id that now names an UNRELATED thread
        self.by_id_opens += 1; self._log('open_thread_by_id:%d' % (tid or 0)); return 9000
    def cancel_sync_io(self, hthread):
        self._log('cancel_sync:%d' % hthread)
        if hthread == 9000: self.unrelated_cancels.append(hthread); return True
        self.sync_cancels.append(self.thread_handles.get(hthread))
        if self.cancel_sync_fails: return False
        if self.behavior == 'stuck_reader' and self.thread_handles.get(hthread) == 'job-stdout-reader':
            self.release.set()                                     # documented sync cancel unblocks it
        return True
    def read(self, h, n):
        # contract: bytes, or None at EOF. b'' is a successful empty read (NOT EOF).
        if self.behavior == 'flood': return b'x' * n
        if self.behavior == 'echo':
            if getattr(self, '_echoed', False): return None
            self._echoed = True
            return bytes(self.sent) or None
        if self.behavior in ('stuck_reader', 'stuck_forever'):
            self.release.wait(10); raise w.IsolationError('read cancelled')
        if self.behavior == 'descendant':
            if self.payload is not None:
                d, self.payload = self.payload, None; return d     # main process wrote its output
            if not self.killed:
                self.release.wait(10)                              # descendant holds the write end: no EOF
            return None                                            # job killed -> descendant dead -> EOF
        if self.payload is None: return None
        d, self.payload = self.payload, None; return d
    def write(self, h, data):
        if self.writer == 'error': raise w.IsolationError('WriteFile failed')
        if self.writer == 'zero': raise w.IsolationError('WriteFile made no progress.')
        self.sent += data; return len(data)
    def close(self, h):
        self._log('close:%d' % h); self.closed.append(h)
        if h == self.job_handle: self.job_closed = True            # last job handle: KILL_ON_JOB_CLOSE fires
        return h not in self.close_fail


def run(fake, data=b'in', **kw):
    return w.run_in_job(['python.exe', '-m', 'worker'], data, env={'A': 'b'}, cwd='.', api=fake, **kw)


def closes_of(fake, handle):
    return [i for i, c in enumerate(fake.calls) if c == 'close:%d' % handle]


def test_order_job_then_suspended_then_assign_then_resume():
    f = Fake(); assert run(f) == b'{}'
    c = f.calls
    assert c.index('job') < c.index('create_suspended') < c.index('assign') < c.index('resume')


def test_assign_failure_never_resumes_kills_and_waits_before_closing():
    f = Fake(assign_fails=True, behavior='hang')
    with pytest.raises(w.IsolationError): run(f)
    c = f.calls
    assert 'resume' not in c and c.count('terminate_process') == 1
    t = c.index('terminate_process')
    first_close = min(i for i, x in enumerate(c) if x.startswith('close:'))
    assert 'wait' in c[t:first_close], 'must wait for the killed process before closing handles'


def test_assign_failure_reap_timeout_is_reported_unknown_and_killed_once(monkeypatch):
    # The wait for the terminated child never confirms exit (hang + timeout):
    # failure/unknown, never proof of death; the kill must be issued exactly once.
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.05)
    f = Fake(assign_fails=True, behavior='hang', never_exits=True)
    with pytest.raises(w.IsolationError, match='could not be confirmed'): run(f)
    assert f.calls.count('terminate_process') == 1


def test_assign_failure_reap_error_is_reported_unknown():
    # The wait itself errors (e.g. access denied on the handle): still unknown,
    # never treated as proof of death.
    f = Fake(assign_fails=True, wait_error=True)
    with pytest.raises(w.IsolationError, match='could not be confirmed'): run(f)


def test_run_stage_kill_exit_unconfirmed_is_reported(monkeypatch):
    # Process killed at the wall deadline but the confirmation wait errors:
    # reported as unconfirmed, and cleanup must not silently proceed.
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.05)
    f = Fake(behavior='hang', wait_error=True)
    with pytest.raises(w.IsolationError, match='could not be confirmed'): run(f, wall=0.1)


def test_run_stage_reap_timeout_after_kill_is_reported(monkeypatch):
    # Wall deadline hits, the job is killed, but the confirmation wait never
    # confirms exit: reported as unconfirmed, never treated as proof of death.
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.05)
    f = Fake(behavior='hang', never_exits=True)
    with pytest.raises(w.IsolationError, match='could not be confirmed'): run(f, wall=0.1)
    assert 'terminate_job' in f.calls


def test_process_and_thread_handles_closed_before_job_handle():
    # The job handle is the KILL_ON_JOB_CLOSE backstop: it must close LAST.
    f = Fake(); run(f)
    last = f.calls[max(i for i, c in enumerate(f.calls) if c.startswith('close:'))]
    assert last == 'close:%d' % 101, 'job handle (first created) must be the last close'


def test_io_threads_are_owned_and_non_daemon():
    # Explicit ownership: no fire-and-forget daemon threads may be created.
    created = []
    real = threading.Thread
    class Spy(real):
        def __init__(self, *a, **k):
            created.append(k)
            super().__init__(*a, **k)
    orig = threading.Thread
    threading.Thread = Spy
    try:
        f = Fake(); assert run(f) == b'{}'
    finally:
        threading.Thread = orig
    assert len(created) == 2
    assert all(k.get('daemon') is False for k in created), 'I/O threads must not be daemon'
    assert all(k.get('name') for k in created), 'I/O threads must be named'


def test_all_handles_closed_exactly_once_on_success_and_failure():
    for f in (Fake(), Fake(exit_code=1), Fake(assign_fails=True)):
        try: run(f)
        except w.IsolationError: pass
        assert len(f.closed) == len(set(f.closed)), 'double close'
        assert len(set(f.closed)) >= 6


def test_wall_timeout_kills_job_and_fails_closed():
    f = Fake(behavior='hang')
    with pytest.raises(w.IsolationError, match='budget'): run(f, wall=0.1)
    assert 'terminate_job' in f.calls


def test_wall_clock_includes_setup():
    f = Fake(); orig = f.create_suspended
    def slow(*a): time.sleep(0.3); return orig(*a)
    f.create_suspended = slow
    with pytest.raises(w.IsolationError, match='budget'): run(f, wall=0.1)
    assert 'resume' not in f.calls, 'budget spent during setup: child must never be resumed'


def test_ceiling_documented_budget_holds(monkeypatch):
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.2); monkeypatch.setattr(w, 'JOIN_WAIT_SECONDS', 0.2)
    f = Fake(behavior='stuck_forever'); t = time.monotonic()
    with pytest.raises(w.IsolationError): run(f, wall=0.2)
    ceiling = 0.2 + w.POLL_SECONDS + 0.2 + 0.2
    assert time.monotonic() - t < ceiling + 0.5   # +0.5 scheduler slack
    f.release.set()


def test_blocked_reader_is_cancelled_and_handle_closed_after_thread_ends(monkeypatch):
    monkeypatch.setattr(w, 'JOIN_WAIT_SECONDS', 1.0)
    f = Fake(behavior='stuck_reader')
    with pytest.raises(w.IsolationError): run(f, wall=0.1)
    assert any(c.startswith('cancel_io:') for c in f.calls)
    assert f.out_r in f.closed


def test_uncancellable_reader_handle_is_not_closed_under_it(monkeypatch):
    monkeypatch.setattr(w, 'JOIN_WAIT_SECONDS', 0.3)
    f = Fake(behavior='stuck_forever')
    with pytest.raises(w.IsolationError, match='did not stop'): run(f, wall=0.1)
    assert f.out_r not in f.closed      # single owner: never close a handle another thread is blocked on
    f.release.set()


# ---- Rev 4, writer-review claim 1: synchronous pipe I/O needs the ----
# ---- documented synchronous cancel path, not only CancelIoEx      ----
def test_stuck_reader_is_released_by_synchronous_cancel(monkeypatch):
    # ReadFile docs: CancelIo/CancelIoEx cancel ASYNCHRONOUS I/O;
    # 'Use CancelSynchronousIo to cancel pending synchronous I/O operations.'
    # Our pipe I/O is synchronous (lpOverlapped NULL, no FILE_FLAG_OVERLAPPED),
    # so the blocked thread's own handle must be cancelled. Fails on rev 3,
    # which never calls CancelSynchronousIo.
    monkeypatch.setattr(w, 'JOIN_WAIT_SECONDS', 1.0)
    f = Fake(behavior='stuck_reader')
    with pytest.raises(w.IsolationError, match='budget'): run(f, wall=0.1)
    assert f.sync_cancels, 'CancelSynchronousIo must be attempted on the blocked thread'
    assert f.out_r in f.closed      # the thread ended, then its handle was closed


def test_sync_cancel_unavailable_falls_back_to_cancelioex(monkeypatch):
    # OpenThread can fail (e.g. access denied): CancelIoEx on the pipe end
    # remains as the fallback and the thread still stops.
    monkeypatch.setattr(w, 'JOIN_WAIT_SECONDS', 1.0)
    f = Fake(behavior='stuck_reader', cancel_sync_fails=True)
    with pytest.raises(w.IsolationError, match='budget'): run(f, wall=0.1)
    assert any(c.startswith('cancel_io:') for c in f.calls)
    assert f.out_r in f.closed


def test_confirmed_cleanup_closes_job_last_and_needs_no_emergency_note():
    # The two protocols stay separate: a run with a confirmed exit (here the
    # normal success path) closes job LAST and raises nothing; a killed run
    # with a confirmed kill (hang behaviour) raises the budget error with NO
    # emergency-close note attached.
    f = Fake(); assert run(f) == b'{}'
    assert f.closed[-1] == f.job_handle
    f2 = Fake(behavior='hang')
    with pytest.raises(w.IsolationError, match='budget') as ei: run(f2, wall=0.1)
    assert 'retained' not in str(ei.value)
    assert f2.closed[-1] == f2.job_handle


# ---- Rev 4, writer-review claim 3: normal exit must still terminate the job ----
def test_normal_exit_terminates_job_so_descendant_releases_stdout(monkeypatch):
    # The main process exits cleanly (wait confirms), but a DESCENDANT inherited
    # the stdout write end: the reader sees no EOF until the job - and with it
    # the descendant - is killed. Rev 3 skipped TerminateJobObject on a normal
    # exit and stalled in the join, then failed the run. Fails on rev 3.
    monkeypatch.setattr(w, 'JOIN_WAIT_SECONDS', 0.5)
    f = Fake(behavior='descendant')
    assert run(f) == b'{}'
    c = f.calls
    assert 'terminate_job' in c, 'the job must be terminated even after a normal exit'
    assert c.index('terminate_job') < max(i for i, x in enumerate(c) if x.startswith('close:')), \
        'job termination must precede handle teardown'
    assert not any(x.startswith(('cancel_io:', 'cancel_sync:')) for x in c), \
        'EOF after the job kill must reach the reader before any cancel is needed'


def test_output_overflow_kills_and_fails_closed():
    f = Fake(behavior='flood')
    with pytest.raises(w.IsolationError, match='exceeds'): run(f, output_limit=1000)
    assert 'terminate_job' in f.calls


def test_nonzero_exit_fails_closed():
    with pytest.raises(w.IsolationError): run(Fake(exit_code=3))


def test_writer_error_rejects_incomplete_input():
    with pytest.raises(w.IsolationError, match='input'): run(Fake(writer='error'))


def test_reader_error_rejects_incomplete_output():
    class RErr(Fake):
        def read(self, h, n): raise w.IsolationError('ReadFile failed (5).')
    with pytest.raises(w.IsolationError, match='incomplete'): run(RErr())


def test_zero_byte_write_progress_rejects():
    class ZeroFake(Fake):
        def write(self, h, data): return 0      # a misbehaving api returning 0 without raising
    # run_in_job loops on api.write; zero progress must not spin forever or succeed
    f = ZeroFake()
    t = time.monotonic()
    with pytest.raises(w.IsolationError): run(f, wall=0.3)
    assert time.monotonic() - t < 6


def test_fake_stdin_roundtrip_bytes_identical():
    data = bytes(range(256)) * 1000
    f = Fake(behavior='echo'); assert run(f, data) == data and bytes(f.sent) == data


def test_oversize_input_refused_before_any_api_call():
    f = Fake()
    with pytest.raises(w.IsolationError): w.run_in_job(['x'], b'x' * (w.INPUT_BYTES + 1), env={}, cwd='.', api=f)
    assert f.calls == []


def test_command_line_is_mutable_owned_unicode_buffer():
    b = w.make_command_line_buffer(['C:\\Py thon\\python.exe', '-m', 'wé'])
    assert isinstance(b, ctypes.Array) and b._type_ is ctypes.c_wchar
    assert b.value.startswith('"C:\\Py thon\\python.exe"')
    b[0] = 'X'                                      # writable
    assert b.value.startswith('XC:')
    with pytest.raises(w.IsolationError): w.make_command_line_buffer(['a\0b'])
    with pytest.raises(w.IsolationError): w.make_command_line_buffer(['a' * w.MAX_CMDLINE_CHARS])


def test_limits_values_and_flags():
    l = w.build_limits(); b = l.BasicLimitInformation
    assert b.PerProcessUserTimeLimit == 20_000_000 and l.ProcessMemoryLimit == 512 * 2**20
    assert b.ActiveProcessLimit == 1 and b.LimitFlags == w.REQUIRED_FLAGS
    assert not b.LimitFlags & 0x800 and not b.LimitFlags & 0x1000   # no breakaway flags


def test_active_process_limit_zero_lifts_flag_for_orphan_test():
    l = w.build_limits(active_process_limit=0); b = l.BasicLimitInformation
    assert b.ActiveProcessLimit == 0
    assert not b.LimitFlags & w.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    assert b.LimitFlags & w.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE


# ---- PID helper: tri-state, access denied is never proof of death -----------
class FakePidApi:
    def __init__(self, handle=500, err=0, exit_code=w.STILL_ACTIVE, getexit_ok=True):
        self.handle = handle; self.err_v = err; self.exit_code = exit_code
        self.getexit_ok = getexit_ok; self.closed = []
    def open_process(self, pid, access=w.PROCESS_QUERY_LIMITED_INFORMATION):
        return self.handle
    def err(self): return self.err_v
    def GetExitCodeProcess(self, h, p):
        if not self.getexit_ok: return 0
        p._obj.value = self.exit_code
        return 1
    def close(self, h): self.closed.append(h); return True


def test_pid_state_alive_when_still_active():
    assert w.pid_state(1234, api=FakePidApi()) == w.PID_ALIVE


def test_open_process_failure_is_unknown_for_any_error():
    # Rev 4 (writer claim 4, verified against the OpenProcess doc page): the ONLY
    # documented ERROR_INVALID_PARAMETER case is pid 0 (which EXISTS); System/
    # CSRSS processes fail with ERROR_ACCESS_DENIED (they EXIST). No error is
    # documented to mean 'no such pid' for an arbitrary positive pid, so every
    # OpenProcess failure is 'unknown', including error 87. Rev 3 wrongly
    # reported 'dead' for 87 - this test fails on rev 3.
    assert w.pid_state(1234, api=FakePidApi(handle=None, err=w.ERROR_INVALID_PARAMETER)) == w.PID_UNKNOWN


def test_pid_state_unknown_when_access_denied():
    # The process EXISTS but may not be queried: unknown, never 'dead'.
    assert w.pid_state(1234, api=FakePidApi(handle=None, err=w.ERROR_ACCESS_DENIED)) == w.PID_UNKNOWN


def test_pid_state_closes_the_opened_handle_on_the_query_path():
    a = FakePidApi(exit_code=0)
    assert w.pid_state(1234, api=a) == w.PID_DEAD   # opened + finished code: the only established 'dead'
    assert a.closed == [500]


def test_pid_state_unknown_on_other_open_failures():
    assert w.pid_state(1234, api=FakePidApi(handle=None, err=31)) == w.PID_UNKNOWN   # ERROR_GEN_FAILURE


def test_handle_state_tri_state_on_a_retained_handle():
    # The reliable pattern for descendants: open a handle BEFORE the kill and
    # query it after; no pid-to-error guessing is involved.
    assert w.handle_state(500, FakePidApi()) == w.PID_ALIVE
    assert w.handle_state(500, FakePidApi(exit_code=0)) == w.PID_DEAD
    assert w.handle_state(500, FakePidApi(getexit_ok=False)) == w.PID_UNKNOWN


def test_pid_state_unknown_when_exit_query_fails():
    assert w.pid_state(1234, api=FakePidApi(getexit_ok=False)) == w.PID_UNKNOWN


# =====================================================================
# REV 5 tests. Each one FAILS on the rev 4 module (checked by execution).
# =====================================================================
def _bare_api(**stubs):
    """WinApi instance without loading kernel32: methods run on Linux against stubs."""
    a = w.WinApi.__new__(w.WinApi); a.err = lambda: 0
    for k, v in stubs.items(): setattr(a, k, v)
    return a


# ---- defect 1: stable thread identity (handle, not id) ----
def test_r5_cancel_uses_handle_the_thread_duplicated_to_itself(monkeypatch):
    monkeypatch.setattr(w, 'JOIN_WAIT_SECONDS', 1.0)
    f = Fake(behavior='stuck_reader')
    with pytest.raises(w.IsolationError): run(f, wall=0.1)
    assert f.by_id_opens == 0, 'a thread must never be opened by its (reusable) native id'
    assert f.unrelated_cancels == [], 'cancel must never reach a handle for some other thread'
    assert 'job-stdout-reader' in f.sync_cancels, 'cancel must go through the reader\'s own held handle'
    assert sorted(f.thread_handles.values()) == ['job-stdin-writer', 'job-stdout-reader']


def test_r5_each_thread_publishes_its_handle_before_any_io():
    class Strict(Fake):
        def _chk(self):
            assert threading.current_thread().name in self.thread_handles.values(), 'I/O before handle published'
        def read(self, h, n): self._chk(); return super().read(h, n)
        def write(self, h, d): self._chk(); return super().write(h, d)
    assert run(Strict()) == b'{}'


def test_r5_thread_handles_closed_once_and_only_after_thread_ended(monkeypatch):
    f = Fake(); assert run(f) == b'{}'
    assert len(f.thread_handles) == 2
    for h in f.thread_handles: assert f.closed.count(h) == 1
    monkeypatch.setattr(w, 'JOIN_WAIT_SECONDS', 0.3)
    g = Fake(behavior='stuck_forever')
    with pytest.raises(w.IsolationError, match='did not stop'): run(g, wall=0.1)
    live = [h for h, n in g.thread_handles.items() if n == 'job-stdout-reader']
    assert live and not any(h in g.closed for h in live), 'handle closed under a live thread'
    g.release.set()


def test_r5_handle_publication_failure_fails_closed():
    class NoDup(Fake):
        def open_current_thread(self): raise w.IsolationError('DuplicateHandle failed')
    f = NoDup()
    with pytest.raises(w.IsolationError): run(f)
    assert 'terminate_job' in f.calls


def test_r5_open_current_thread_wrapper_contract():
    seen = {}
    def dup(src, sh, tgt, out, access, inh, opts):
        seen.update(access=access, inh=inh, opts=opts); out._obj.value = 1234; return 1
    a = _bare_api(GetCurrentProcess=lambda: -1, GetCurrentThread=lambda: -2, DuplicateHandle=dup)
    assert a.open_current_thread() == 1234
    assert seen == {'access': w.THREAD_TERMINATE, 'inh': 0, 'opts': 0}
    a2 = _bare_api(GetCurrentProcess=lambda: -1, GetCurrentThread=lambda: -2, DuplicateHandle=lambda *x: 0)
    with pytest.raises(w.IsolationError): a2.open_current_thread()


# ---- defect 2: TerminateJobObject result ----
def test_r5_failed_job_termination_after_normal_exit_is_a_failed_run():
    f = Fake(terminate_job_ok=False)
    with pytest.raises(w.IsolationError, match='TerminateJobObject failed'): run(f)
    assert 'terminate_process' in f.calls, 'fallback kill of the main process must be attempted'
    assert f.job_handle not in f.closed and f.proc not in f.closed, 'unproven job tree: handles retained'
    assert w.retained_count() == 1


def test_r5_failed_job_termination_on_wall_kill_is_reported_and_retained(monkeypatch):
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.05)
    f = Fake(behavior='hang', terminate_job_ok=False, never_exits=True)
    with pytest.raises(w.IsolationError, match='could not be confirmed') as ei: run(f, wall=0.1)
    assert 'retained' in str(ei.value)
    assert f.job_handle not in f.closed and f.proc not in f.closed


# ---- design change: no closing controls without confirmed exit ----
def test_r5_unconfirmed_exit_retains_control_handles_never_closes_them(monkeypatch):
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.05)
    f = Fake(behavior='hang', never_exits=True)
    with pytest.raises(w.IsolationError, match='retained') as ei: run(f, wall=0.1)
    assert 'not proof of death' in str(ei.value)
    assert not {f.proc, f.thr, f.job_handle} & set(f.closed), 'control handle closed without confirmed exit'
    assert w.retained_count() == 1 and len(f.closed) == len(set(f.closed))


def test_r5_reap_closes_only_after_confirmed_exit_job_last(monkeypatch):
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.05); monkeypatch.setattr(w, 'REAP_WAIT_SECONDS', 0.05)
    f = Fake(behavior='hang', never_exits=True)
    with pytest.raises(w.IsolationError): run(f, wall=0.1)
    assert w.reap_retained() == 1 and not {f.proc, f.thr, f.job_handle} & set(f.closed)   # still unconfirmed
    f.never_exits = False; f.behavior = 'ok'                                              # exit now confirmable
    assert w.reap_retained() == 0
    tail = [h for h in f.closed if h in (f.proc, f.thr, f.job_handle)]
    assert tail == [f.thr, f.proc, f.job_handle]


def test_r5_reap_retries_failed_job_termination(monkeypatch):
    f = Fake(terminate_job_ok=False)
    with pytest.raises(w.IsolationError): run(f)
    assert w.reap_retained() == 1                      # still failing: stays retained
    f.terminate_job_ok = True
    assert w.reap_retained() == 0 and f.job_handle in f.closed


def test_r5_retention_is_bounded_and_refuses_new_runs(monkeypatch):
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.05); monkeypatch.setattr(w, 'REAP_WAIT_SECONDS', 0.05)
    monkeypatch.setattr(w, 'MAX_RETAINED', 1)
    f = Fake(behavior='hang', never_exits=True)
    with pytest.raises(w.IsolationError): run(f, wall=0.1)
    g = Fake()
    with pytest.raises(w.IsolationError, match='Too many unreaped'): run(g)
    assert g.calls == [], 'a refused run must not touch the API'


def test_r5_assign_failure_with_failed_terminate_process_retains_never_assigned_child(monkeypatch):
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.05)
    f = Fake(assign_fails=True, terminate_process_ok=False, never_exits=True, behavior='hang')
    with pytest.raises(w.IsolationError, match='could not be confirmed') as ei: run(f)
    assert 'TerminateProcess also failed' in str(ei.value) and 'retained' in str(ei.value)
    assert f.proc not in f.closed and f.job_handle not in f.closed and 'resume' not in f.calls
    g = Fake(assign_fails=True, terminate_process_ok=False)       # exit confirmable, but the kill call failed
    with pytest.raises(w.IsolationError, match='TerminateProcess reported failure'): run(g)


# ---- partial thread start ----
@pytest.mark.parametrize('fail_name', ['job-stdin-writer', 'job-stdout-reader'])
def test_r5_thread_start_failure_is_clean_and_owned(monkeypatch, fail_name):
    orig = threading.Thread.start
    def start(self):
        if self.name == fail_name: raise RuntimeError("can't start new thread")
        orig(self)
    monkeypatch.setattr(threading.Thread, 'start', start)
    f = Fake()
    with pytest.raises(w.IsolationError, match='Could not start I/O thread'): run(f)
    assert 'terminate_job' in f.calls, 'the resumed child must be killed'
    assert f.proc in f.closed and f.job_handle in f.closed and w.retained_count() == 0
    assert len(f.closed) == len(set(f.closed))


# ---- return-value contracts ----
def test_r5_closehandle_failure_is_reported_not_ignored():
    f = Fake(close_fail=[101])     # 101 = the job handle
    with pytest.raises(w.IsolationError, match='CloseHandle failed'): run(f)


def test_r5_resume_requires_previous_suspend_count_exactly_one():
    assert _bare_api(ResumeThread=lambda t: 1).resume(5) is None
    for bad in (0, 2, 0xFFFFFFFF):
        with pytest.raises(w.IsolationError): _bare_api(ResumeThread=lambda t, b=bad: b).resume(5)


def test_r5_readfile_eof_is_only_broken_pipe():
    def ok_empty(h, buf, n, got, ov): got._obj.value = 0; return 1
    assert _bare_api(ReadFile=ok_empty).read(1, 10) == b''        # success, no data: NOT EOF
    a = _bare_api(ReadFile=lambda *x: 0); a.err = lambda: w.ERROR_BROKEN_PIPE
    assert a.read(1, 10) is None                                  # EOF
    b = _bare_api(ReadFile=lambda *x: 0); b.err = lambda: 5
    with pytest.raises(w.IsolationError): b.read(1, 10)


def test_r5_empty_successful_read_is_not_eof():
    class Gappy(Fake):
        def __init__(self): super().__init__(); self.seq = [b'', b'', b'ab', None]
        def read(self, h, n): return self.seq.pop(0)
    assert run(Gappy()) == b'ab'
    class Endless(Fake):
        def read(self, h, n): return b''
    with pytest.raises(w.IsolationError): run(Endless())


def test_r5_attribute_list_sizing_call_must_report_a_size():
    def init(buf, *a): return 0 if buf is None else 1      # sizing call leaves size at 0
    a = _bare_api(InitializeProcThreadAttributeList=init, UpdateProcThreadAttribute=lambda *x: 1,
                  DeleteProcThreadAttributeList=lambda *x: None, CreateProcessW=lambda *x: 1)
    with pytest.raises(w.IsolationError, match='no size'):
        a.create_suspended(['python.exe'], b'\0\0', '.', 1, 2, 3)


# ---- timing: each cleanup phase at most once; computed ceiling ----
def test_r5_cleanup_phases_run_at_most_once_and_ceiling_holds(monkeypatch):
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.2); monkeypatch.setattr(w, 'JOIN_WAIT_SECONDS', 0.2)
    waits, stops = [], []
    ow, os_ = w._wait_exit, w._stop_io_threads
    monkeypatch.setattr(w, '_wait_exit', lambda *a, **k: (waits.append(1), ow(*a, **k))[1])
    monkeypatch.setattr(w, '_stop_io_threads', lambda *a, **k: (stops.append(1), os_(*a, **k))[1])
    f = Fake(behavior='stuck_forever', never_exits=True)
    t = time.monotonic()
    with pytest.raises(w.IsolationError): run(f, wall=0.2)
    elapsed = time.monotonic() - t
    assert len(waits) == 1, 'exactly one confirmation wait after the deadline (rev 4 emergency close added a second)'
    assert len(stops) == 1, 'exactly one I/O stop window'
    assert elapsed < 0.2 + w.POLL_SECONDS + 0.2 + 0.2 + 0.5     # WALL + POLL + KILL_WAIT + JOIN_WAIT + slack
    f.release.set()



# =====================  REV 6: retained-owner lifecycle + admission  =====================
# Handle ids from Fake: job 101, stdin read/write 102/103, stdout read/write 104/105,
# nul 106, process 107, primary thread 108, I/O thread self-handles 7000 (writer), 7001 (reader).
JOB, IN_W, OUT_R, PROC, THR = 101, 103, 104, 107, 108


def _open_ids(e):
    hs = [e.thread, e.proc, e.job] + list(e.io_handles)
    ids = {h.h for h in hs if h is not None and h.h}
    ids |= {s.h for s in e.slots if s.h}
    return ids


def _retain_unconfirmed(monkeypatch, **kw):
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.05); monkeypatch.setattr(w, 'REAP_WAIT_SECONDS', 0.05)
    f = Fake(behavior='hang', never_exits=True, **kw)
    with pytest.raises(w.IsolationError): run(f, wall=0.1)
    assert w.retained_count() == 1
    return f


# ---- rev 7: identity quarantine. After the FIRST ambiguous failure on a numeric identity
# ---- nothing (close, cancel, wait, terminate) is ever called on that number again.
class Rec(Fake):
    """Fake that records (op, handle) so tests can prove which numbers were touched."""
    def __init__(self, *a, err_code=5, **kw):
        super().__init__(*a, **kw); self.ops = []; self.err_code = err_code; self.invalid_wait = set()
    def err(self): return self.err_code
    def wait(self, p, ms):
        self.ops.append(('wait', p))
        if p in self.invalid_wait: raise w.InvalidHandleError('invalid')
        return super().wait(p, ms)
    def terminate_job(self, j): self.ops.append(('terminate_job', j)); return super().terminate_job(j)
    def terminate_process(self, p): self.ops.append(('terminate_process', p)); return super().terminate_process(p)
    def close(self, h): self.ops.append(('close', h)); return super().close(h)
    def cancel_io(self, h): self.ops.append(('cancel_io', h)); return super().cancel_io(h)
    def cancel_sync_io(self, h): self.ops.append(('cancel_sync', h)); return super().cancel_sync_io(h)
    def touched(self, h, ops=None): return [o for o, x in self.ops if x == h and (ops is None or o in ops)]


class FlipFake(Fake):
    """close(): FALSE on the first call, TRUE afterwards (the rev 6 two-call counterexample:
    the second TRUE may close a DIFFERENT kernel object that reused the number)."""
    def __init__(self, *a, **kw): super().__init__(*a, **kw); self.n_close = 0
    def close(self, h):
        self.n_close += 1; self.calls.append('close:%d' % h)
        return self.n_close > 1


def test_r7_two_call_counterexample_handle_close_is_never_retried():
    f = FlipFake(); h = w._Handle(f, 5)
    assert h.close() is False
    assert h.close() is False, 'rev 6 returned True here: a second CloseHandle on an uncertain number'
    assert f.n_close == 1 and h.is_open and h.quarantined and h.value() is None


def test_r7_two_call_counterexample_thread_slot_close_is_never_retried():
    f = FlipFake(); s = w._ThreadSlot(); s.h = 7000
    assert s.close(f) is False and s.close(f) is False
    assert f.n_close == 1 and s.is_open and s.quarantined and s.cancel(f) is None


def test_r7_quarantined_handle_is_never_cancelled_or_valued():
    f = Rec(close_fail=[5]); h = w._Handle(f, 5)
    assert h.close() is False
    h.cancel()
    assert f.touched(5) == ['close'] and h.value() is None and h.is_open


@pytest.mark.parametrize('hid', [IN_W, OUT_R, 7000, PROC, THR, JOB, 102])
def test_r7_failed_close_is_called_once_ever_stays_counted_and_blocks_success(hid):
    f = Rec(close_fail=[hid])
    with pytest.raises(w.IsolationError, match='CloseHandle failed'): run(f)
    assert w.retained_count() == 1
    f.close_fail.clear()                      # the fake now WOULD return TRUE on a retry
    for _ in range(5): w.reap_retained()
    assert f.touched(hid, ['close']) == ['close'], 'a failed identity must never be closed again'
    assert w.retained_count() == 1, 'capacity stays reserved: the entry is never freed'
    e = w._RETAINED[0]
    assert e.quarantined, 'diagnostic names the quarantined handle'


def test_r7_reap_one_never_reuses_a_process_handle_whose_close_failed(monkeypatch):
    f = Rec(behavior='hang', never_exits=True)
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.05); monkeypatch.setattr(w, 'REAP_WAIT_SECONDS', 0.05)
    with pytest.raises(w.IsolationError): run(f, wall=0.1)
    f.never_exits = False; f.behavior = 'ok'; f.close_fail.add(PROC)
    assert w.reap_retained() == 1                       # close(PROC) fails once -> quarantined
    f.close_fail.clear(); mark = len(f.ops)
    for _ in range(4): assert w.reap_retained() == 1
    assert [o for o in f.ops[mark:] if o[1] == PROC] == [], 'wait/terminate/close on a quarantined proc'
    assert JOB not in [x for o, x in f.ops[mark:]]


def test_r7_reap_one_never_reuses_job_after_invalid_handle_report(monkeypatch):
    f = Rec(terminate_job_ok=False, err_code=w.ERROR_INVALID_HANDLE)
    with pytest.raises(w.IsolationError): run(f)
    assert w.retained_count() == 1
    mark = len(f.ops); f.terminate_job_ok = True; f.err_code = 5
    for _ in range(4): w.reap_retained()
    assert [o for o in f.ops[mark:] if o[1] == JOB] == [], 'invalid job identity must not be reused'
    assert w.retained_count() == 1 and 'job' in w._RETAINED[0].quarantined


def test_r7_invalid_process_handle_in_reaper_stops_waits_and_keeps_other_controls_open(monkeypatch):
    f = Rec(behavior='hang', never_exits=True)
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.05); monkeypatch.setattr(w, 'REAP_WAIT_SECONDS', 0.05)
    with pytest.raises(w.IsolationError): run(f, wall=0.1)
    f.invalid_wait.add(PROC); w.reap_retained(); mark = len(f.ops)
    f.invalid_wait.clear(); f.never_exits = False
    for _ in range(3): assert w.reap_retained() == 1
    assert [o for o in f.ops[mark:] if o[1] == PROC] == []
    assert 'close' not in f.touched(JOB) and 'close' not in f.touched(THR), 'exit never confirmed: fail closed'


def test_r7_main_loop_invalid_process_handle_is_unknown_not_death_and_not_reused():
    f = Rec(); f.invalid_wait.add(PROC)
    with pytest.raises(w.IsolationError): run(f)
    assert f.touched(PROC, ['wait']) == ['wait'], 'only the one wait that reported invalid'
    assert f.touched(PROC, ['close']) == [] and w.retained_count() == 1


def test_r7_no_retry_constant_remains():
    assert not hasattr(w, 'MAX_CLOSE_ATTEMPTS')



# ---- gap 2: assigned vs unassigned, direct termination retried ----
def test_r6_unassigned_child_direct_termination_is_retried_by_reaper(monkeypatch):
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.05); monkeypatch.setattr(w, 'REAP_WAIT_SECONDS', 0.05)
    f = Fake(assign_fails=True, behavior='hang', never_exits=True, terminate_process_ok=False)
    with pytest.raises(w.IsolationError, match='retained'): run(f)
    assert f.calls.count('terminate_process') == 1
    e = w._RETAINED[0]
    assert e.assigned is False and e.term_ok is False, 'an empty job is not containment'
    w.reap_retained(); w.reap_retained()
    assert f.calls.count('terminate_process') == 3, 'every reap must retry the direct kill'
    assert 'terminate_job' not in f.calls and not {PROC, THR, JOB} & set(f.closed)
    f.terminate_process_ok = True; f.never_exits = False; f.behavior = 'ok'
    assert w.reap_retained() == 0
    assert [h for h in f.closed if h in (THR, PROC, JOB)] == [THR, PROC, JOB]


def test_r6_assigned_state_is_recorded(monkeypatch):
    f = _retain_unconfirmed(monkeypatch)
    assert w._RETAINED[0].assigned is True


def test_r6_unassigned_closed_only_after_confirmed_exit(monkeypatch):
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.05)
    f = Fake(assign_fails=True, behavior='hang', never_exits=True)
    with pytest.raises(w.IsolationError): run(f)
    assert w.retained_count() == 1 and not {PROC, THR, JOB} & set(f.closed)


# ---- gap 3: admission reservation and exclusive reaping ----
def test_r6_concurrent_admission_never_exceeds_cap(monkeypatch):
    monkeypatch.setattr(w, 'MAX_RETAINED', 2)
    gate = threading.Event(); results = []; lock = threading.Lock()
    class Gated(Fake):
        def create_suspended(self, *a):
            gate.wait(1.5); return super().create_suspended(*a)
    def go():
        try: run(Gated()); r = 'ok'
        except w.IsolationError as e: r = 'refused' if 'Too many' in str(e) else 'other:%s' % e
        with lock: results.append(r)
    ts = [threading.Thread(target=go) for _ in range(4)]
    for t in ts: t.start()
    deadline = time.monotonic() + 1.0
    while results.count('refused') < 2 and time.monotonic() < deadline: time.sleep(0.01)
    gate.set()
    for t in ts: t.join()
    assert sorted(results) == ['ok', 'ok', 'refused', 'refused'], results
    assert w.active_count() == 0, 'reservations must all be released'


def test_r6_reservation_released_on_failure_and_refusal(monkeypatch):
    monkeypatch.setattr(w, 'MAX_RETAINED', 1)
    with pytest.raises(w.IsolationError): run(Fake(exit_code=1))
    assert w.active_count() == 0 and w.retained_count() == 0
    run(Fake())                                          # slot is free again


def test_r6_reservation_converts_to_retained_entry_without_double_count(monkeypatch):
    monkeypatch.setattr(w, 'MAX_RETAINED', 2)
    _retain_unconfirmed(monkeypatch)
    assert w.retained_count() == 1 and w.active_count() == 0
    run(Fake())                                          # cap 2: one retained + one run is allowed


def test_r6_concurrent_reapers_never_double_close_a_safe_entry(monkeypatch):
    # Writer scenario: a safe retained entry, two reap_retained calls racing; a barrier-like
    # delay inside the first close lets the second reaper overlap.
    f = _retain_unconfirmed(monkeypatch)
    f.never_exits = False; f.behavior = 'ok'
    orig = f.close
    def slow_close(h):
        if h == THR: time.sleep(0.3)
        return orig(h)
    f.close = slow_close
    bar = threading.Barrier(2); rets = []
    def reaper():
        bar.wait(); rets.append(w.reap_retained())
    ts = [threading.Thread(target=reaper) for _ in range(2)]
    for t in ts: t.start()
    for t in ts: t.join()
    for h in (THR, PROC, JOB):
        assert f.calls.count('close:%d' % h) == 1, 'handle %d closed by more than one reaper' % h
    assert w.retained_count() == 0 and w.reap_retained() == 0


def test_r6_reaper_skips_entry_held_by_another_reaper(monkeypatch):
    f = _retain_unconfirmed(monkeypatch)
    e = w._RETAINED[0]
    assert e.lock.acquire(False)
    try: assert w.reap_retained() == 1       # skipped, still counted, no API call
    finally: e.lock.release()
    n = len(f.calls); e.lock.acquire(False)
    try: w.reap_retained(); assert len(f.calls) == n
    finally: e.lock.release()

# Expected layouts are derived by hand from the member TYPES in Microsoft Learn
# (JOBOBJECT_BASIC/EXTENDED_LIMIT_INFORMATION, IO_COUNTERS: LARGE_INTEGER/ULONGLONG 8 bytes,
# 8-byte aligned under MSVC; DWORD 4; SIZE_T/ULONG_PTR pointer-sized), NOT from ctypes.
# Learn does not print sizes or offsets; the OS-level confirmation is the windows_only test below.
X64_BASIC = {'PerProcessUserTimeLimit': 0, 'PerJobUserTimeLimit': 8, 'LimitFlags': 16,
             'MinimumWorkingSetSize': 24, 'MaximumWorkingSetSize': 32, 'ActiveProcessLimit': 40,
             'Affinity': 48, 'PriorityClass': 56, 'SchedulingClass': 60}
X64_EXT = {'BasicLimitInformation': 0, 'IoInfo': 64, 'ProcessMemoryLimit': 112,
           'JobMemoryLimit': 120, 'PeakProcessMemoryUsed': 128, 'PeakJobMemoryUsed': 136}
X86_BASIC = {'PerProcessUserTimeLimit': 0, 'PerJobUserTimeLimit': 8, 'LimitFlags': 16,
             'MinimumWorkingSetSize': 20, 'MaximumWorkingSetSize': 24, 'ActiveProcessLimit': 28,
             'Affinity': 32, 'PriorityClass': 36, 'SchedulingClass': 40}
X86_EXT = {'BasicLimitInformation': 0, 'IoInfo': 48, 'ProcessMemoryLimit': 96,
           'JobMemoryLimit': 100, 'PeakProcessMemoryUsed': 104, 'PeakJobMemoryUsed': 108}
X64_SIZES = {'basic': 64, 'io': 48, 'ext': 144}
X86_SIZES = {'basic': 48, 'io': 48, 'ext': 112}


def _check_layout(basic, ext, sizes):
    assert ctypes.sizeof(w.JobBasicLimit) == sizes['basic']
    assert ctypes.sizeof(w.IoCounters) == sizes['io']
    assert ctypes.sizeof(w.JobExtendedLimit) == sizes['ext']
    for name, off in basic.items(): assert getattr(w.JobBasicLimit, name).offset == off, name
    for name, off in ext.items(): assert getattr(w.JobExtendedLimit, name).offset == off, name


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason='x64 layout')
def test_struct_layout_x64_explicit_offsets():
    _check_layout(X64_BASIC, X64_EXT, X64_SIZES)


@pytest.mark.skipif(not (sys.platform == 'win32' and ctypes.sizeof(ctypes.c_void_p) == 4),
                    reason='x86 layout is only meaningful for 32-bit Windows ctypes (32-bit Linux aligns int64 to 4)')
def test_struct_layout_x86_explicit_offsets():
    _check_layout(X86_BASIC, X86_EXT, X86_SIZES)


def test_environment_allowlist_has_no_secrets():
    env = w.build_environment({'SYSTEMROOT': 'C:\\Windows', 'DB_PASSWORD': 'x', 'PATH': 'evil'}, 'C:\\app')
    assert set(env) == {'PYTHONPATH', 'PYTHONUTF8', 'PYTHONDONTWRITEBYTECODE', 'SYSTEMROOT', 'PATH'}
    assert 'evil' not in env['PATH']
    assert b'D\0B\0_\0' not in w.environment_block(env)


def test_environment_rejects_bad_entries():
    with pytest.raises(w.IsolationError): w.environment_block({'A=B': 'c'})


@pytest.mark.skipif(sys.platform == 'win32', reason='non-Windows fail-closed check')
def test_non_windows_fails_closed_no_fallback():
    with pytest.raises(w.IsolationError): w.WinApi()
    with pytest.raises(w.IsolationError): w.run_in_job(['x'], b'', env={}, cwd='.')


# ---------------- B. Windows-only, UNRUN -------------------------------------
def _real(code, data=b'', **kw):
    return w.run_in_job([sys.executable, '-c', code], data, env=w.build_environment(), cwd='.', **kw)


@windows_only
def test_win_roundtrip_stdout():
    assert _real('import sys;sys.stdout.write("ok")') == b'ok'


@windows_only
def test_win_stdin_roundtrip_1mib_identical():
    data = bytes(range(256)) * 4096          # 1 MiB, exercises multiple 64 KiB writes
    out = _real('import sys;sys.stdout.buffer.write(sys.stdin.buffer.read())', data, wall=10)
    assert out == data


@windows_only
def test_win_child_exits_without_reading_stdin_rejects_incomplete_input():
    with pytest.raises(w.IsolationError, match='input'):
        _real('pass', b'x' * 5_000_000, wall=10)


@windows_only
def test_win_wall_kill():
    with pytest.raises(w.IsolationError, match='budget'): _real('import time;time.sleep(30)', wall=0.5)


@windows_only
def test_win_cpu_limit_kills_busy_loop_before_wall():
    t = time.monotonic()
    with pytest.raises(w.IsolationError): _real('while True: pass', wall=10, cpu_seconds=1)
    assert time.monotonic() - t < 8


@windows_only
def test_win_memory_limit():
    with pytest.raises(w.IsolationError): _real('b=bytearray(900*1024*1024);print(len(b))', memory_bytes=256 * 2**20)


@windows_only
def test_win_output_cap():
    with pytest.raises(w.IsolationError, match='exceeds'):
        _real('import sys;sys.stdout.write("x"*5000000)')


@windows_only
def test_win_no_orphan_processes_after_kill(tmp_path):
    """ActiveProcessLimit is LIFTED (0) for this test only, so the grandchild can
    actually be created. With the production limit of 1 the spawn would be
    blocked outright (covered by test_win_active_process_limit_blocks_spawn),
    which would make an orphan check vacuous.
    Rev 4: liveness is checked through RETAINED HANDLES (opened before the
    kill), never through pid-only OpenProcess error guessing (writer claim 4)."""
    pidfile = tmp_path / 'pids.txt'
    code = ('import os,subprocess,sys,time\n'
            f'f=open({str(pidfile)!r},"w");f.write(str(os.getpid())+"\\n");f.flush()\n'
            'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(60)"])\n'
            'f.write(str(p.pid)+"\\n");f.flush()\n'
            'f.close()\n'
            'time.sleep(60)\n')
    api = w.WinApi()
    handles = []
    try:
        err = []
        def go():
            try: _real(code, wall=1.5, active_process_limit=0)
            except w.IsolationError as e: err.append(e)
        t = threading.Thread(target=go)
        t.start()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if pidfile.exists() and len(pidfile.read_text().split()) == 2:
                break
            time.sleep(0.05)
        pids = [int(x) for x in pidfile.read_text().split()]
        assert len(pids) == 2, 'expected worker pid AND grandchild pid; without both this test is vacuous'
        handles = [api.open_process(p) for p in pids]   # retained BEFORE the kill
        assert all(handles), 'must retain a handle to each process before the kill; pid-only liveness is not proof'
        t.join()
        assert err, 'the run must fail closed at the wall deadline'
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            states = [w.handle_state(h, api) for h in handles]
            if all(s == w.PID_DEAD for s in states):
                break
            time.sleep(0.1)
        states = [w.handle_state(h, api) for h in handles]
        assert w.PID_ALIVE not in states, 'orphan process survived the job kill'
        assert w.PID_UNKNOWN not in states, \
            'could not confirm cleanup via a retained handle: unknown is NOT proof of death; investigate on the device'
    finally:
        for h in handles:
            api.close(h)


@windows_only
def test_win_active_process_limit_blocks_spawn(tmp_path):
    """With the production limit ActiveProcessLimit=1 the worker cannot create a
    second process: the spawn must fail and the worker must error out."""
    marker = tmp_path / 'spawned.txt'
    code = ('import subprocess,sys\n'
            'try:\n'
            '    subprocess.Popen([sys.executable,"-c","pass"])\n'
            f'    open({str(marker)!r},"w").write("spawned")\n'
            'except Exception:\n'
            '    raise SystemExit(42)\n'
            'raise SystemExit(0)\n')
    with pytest.raises(w.IsolationError):
        _real(code, wall=10)                 # default active_process_limit=1
    assert not marker.exists(), 'a second process was spawned under ActiveProcessLimit=1'


@windows_only
def test_win_pid_state_of_exited_process():
    """On a real machine: a finished process may read 'dead' (handle still
    openable) or 'unknown'; a bogus pid must read 'unknown' (rev 4: no error
    code is documented to mean 'no such pid'); neither may read 'alive'.
    A retained handle to the finished process must read 'dead'."""
    api = w.WinApi()
    p = subprocess.Popen([sys.executable, '-c', 'pass'])
    h = api.open_process(p.pid)
    assert h, 'must be able to retain a handle to our own child'
    try:
        p.wait()
        assert w.handle_state(h, api) == w.PID_DEAD
    finally:
        api.close(h)
    assert w.pid_state(p.pid) in (w.PID_DEAD, w.PID_UNKNOWN)   # pid reuse window makes UNKNOWN honest
    assert w.pid_state(0x7FFFFFFF) == w.PID_UNKNOWN


@windows_only
def test_win_child_sees_job_and_limits():
    code = 'import app.windows_job_isolation as w;w.self_check_in_expected_job();print("in-job")'
    env = w.build_environment(pythonpath=str(__import__("pathlib").Path(w.__file__).resolve().parent.parent))
    out = w.run_in_job([sys.executable, "-c", code], b"", env=env, cwd=".")
    assert out.strip() == b"in-job"


@windows_only
def test_win_self_check_fails_outside_job():
    r = subprocess.run([sys.executable, '-c', 'import app.windows_job_isolation as w;w.self_check_in_expected_job()'], capture_output=True)
    assert r.returncode != 0


@windows_only
def test_win_struct_size_accepted_and_reported_by_os():
    """OS-level layout check (not self-equal): Set must accept our size, and Query's ReturnLength
    (bytes the OS wrote) must equal our sizeof; fields must round-trip through the OS."""
    api = w.WinApi(); k = api._k
    k.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p]
    k.QueryInformationJobObject.restype = ctypes.c_int32
    limits = w.build_limits(); job = api.create_job(); api.configure_job(job, limits)   # configure_job fails if the size is wrong
    try:
        out = w.JobExtendedLimit(); ret = ctypes.c_uint32()
        assert k.QueryInformationJobObject(job, 9, ctypes.byref(out), ctypes.sizeof(out), ctypes.byref(ret))
        assert ret.value == ctypes.sizeof(w.JobExtendedLimit) == (144 if ctypes.sizeof(ctypes.c_void_p) == 8 else 112)
        b = out.BasicLimitInformation
        assert (b.LimitFlags, b.PerProcessUserTimeLimit, b.ActiveProcessLimit, out.ProcessMemoryLimit) == \
               (w.REQUIRED_FLAGS, 20_000_000, 1, 512 * 2**20)
    finally:
        api.close(job)


# =====================================================================
# REV 8 tests: error classification AT THE ADAPTER BOUNDARY.
# They use the REAL WinApi methods over stub kernel32 functions that return FALSE and
# set GetLastError, so an adapter that drops the error code fails them. (Fakes that raise
# InvalidHandleError themselves are what hid the rev 7 defect.) Each fails on the rev 7 module
# (checked by execution). Linux, no Windows: these prove classification logic only.
# =====================================================================
INVALID, NOT_FOUND, DENIED = w.ERROR_INVALID_HANDLE, w.ERROR_NOT_FOUND, w.ERROR_ACCESS_DENIED


def _failing_api(fn_name, err, calls=None):
    """Real WinApi (no kernel32) whose kernel function `fn_name` returns FALSE with GetLastError=err."""
    def k(*a):
        if calls is not None: calls.append(a[0])
        return 0
    a = _bare_api(**{fn_name: k}); a.err = lambda: err
    return a


ADAPTER_CALLS = {   # method -> (kernel fn, call)
    'cancel_io': ('CancelIoEx', lambda a: a.cancel_io(5)),
    'cancel_sync_io': ('CancelSynchronousIo', lambda a: a.cancel_sync_io(5)),
    'terminate_job': ('TerminateJobObject', lambda a: a.terminate_job(5)),
    'terminate_process': ('TerminateProcess', lambda a: a.terminate_process(5)),
    'assign': ('AssignProcessToJobObject', lambda a: a.assign(5, 6)),
    'resume': ('ResumeThread', lambda a: a.resume(5)),
    'uninheritable': ('SetHandleInformation', lambda a: a.uninheritable(5)),
    'wait': ('WaitForSingleObject', lambda a: a.wait(5, 10)),
    'exit_code': ('GetExitCodeProcess', lambda a: a.exit_code(5)),
    'read': ('ReadFile', lambda a: a.read(5, 10)),
    'write': ('WriteFile', lambda a: a.write(5, b'x')),
}


def _stub_for(name, err):
    """FALSE/failure value for each kernel function; resume and wait signal failure in-band."""
    if name == 'ResumeThread' or name == 'WaitForSingleObject':
        return lambda *a: 0xFFFFFFFF
    return lambda *a: 0


@pytest.mark.parametrize('method', sorted(ADAPTER_CALLS))
def test_r8_every_adapter_method_raises_invalid_handle_on_error_6(method):
    fn, call = ADAPTER_CALLS[method]
    a = _bare_api(**{fn: _stub_for(fn, INVALID)}); a.err = lambda: INVALID
    with pytest.raises(w.InvalidHandleError):
        call(a)


@pytest.mark.parametrize('method', ['cancel_io', 'cancel_sync_io'])
@pytest.mark.parametrize('err', [NOT_FOUND, DENIED])
def test_r8_cancel_other_errors_are_ordinary_false_never_invalid_handle(method, err):
    fn, call = ADAPTER_CALLS[method]
    a = _bare_api(**{fn: lambda *x: 0}); a.err = lambda: err
    assert call(a) is False                     # 1168 = nothing to cancel right now: ordinary


@pytest.mark.parametrize('method', ['terminate_job', 'terminate_process'])
def test_r8_terminate_other_errors_are_false_and_success_is_true(method):
    fn, call = ADAPTER_CALLS[method]
    a = _bare_api(**{fn: lambda *x: 0}); a.err = lambda: DENIED
    assert call(a) is False
    assert call(_bare_api(**{fn: lambda *x: 1})) is True


@pytest.mark.parametrize('method', ['assign', 'resume', 'uninheritable', 'exit_code', 'write', 'read'])
def test_r8_other_failures_raise_isolation_error_but_not_invalid_handle(method):
    fn, call = ADAPTER_CALLS[method]
    a = _bare_api(**{fn: _stub_for(fn, DENIED)}); a.err = lambda: DENIED
    with pytest.raises(w.IsolationError) as ei: call(a)
    assert not isinstance(ei.value, w.InvalidHandleError)


def test_r9_adapter_create_job_allocates_only_and_configure_never_closes():
    closed = []
    a = _bare_api(CreateJobObjectW=lambda *x: 77, SetInformationJobObject=lambda *x: 0,
                  CloseHandle=lambda h: closed.append(h) or 1)
    assert a.create_job() == 77
    for e, cls in ((INVALID, w.InvalidHandleError), (DENIED, w.IsolationError)):
        a.err = lambda e=e: e
        with pytest.raises(cls): a.configure_job(77, w.build_limits())
    assert closed == [], 'adapter never closes; the owner decides'

# ---- owners over the REAL adapter: two calls lead to ONE kernel call (the writer's counterexample) ----
def test_r8_handle_cancel_over_real_adapter_error_6_quarantines_after_one_call():
    calls = []; h = w._Handle(_failing_api('CancelIoEx', INVALID, calls), 5)
    h.cancel(); h.cancel()
    assert calls == [5] and h.quarantined and h.value() is None and h.is_open


def test_r8_handle_cancel_over_real_adapter_1168_is_ordinary_and_repeatable():
    calls = []; h = w._Handle(_failing_api('CancelIoEx', NOT_FOUND, calls), 5)
    h.cancel(); h.cancel()
    assert calls == [5, 5] and not h.quarantined and h.value() == 5


def test_r8_thread_slot_cancel_over_real_adapter_error_6_quarantines_after_one_call():
    calls = []; api = _failing_api('CancelSynchronousIo', INVALID, calls)
    s = w._ThreadSlot(); s.h = 7000
    assert s.cancel(api) is False and s.cancel(api) is None
    assert calls == [7000] and s.quarantined and s.is_open


def test_r8_thread_slot_cancel_over_real_adapter_1168_is_ordinary():
    calls = []; api = _failing_api('CancelSynchronousIo', NOT_FOUND, calls)
    s = w._ThreadSlot(); s.h = 7000
    assert s.cancel(api) is False and s.cancel(api) is False
    assert calls == [7000, 7000] and not s.quarantined


def test_r8_terminate_tree_over_real_adapter_quarantines_the_reporting_identity():
    jc, pc = [], []
    api = _bare_api(TerminateJobObject=lambda j, c: jc.append(j) or 0, TerminateProcess=lambda p, c: pc.append(p) or 1)
    api.err = lambda: INVALID
    hj, hp = w._Handle(api, 11, 'job'), w._Handle(api, 12, 'process')
    ok, err = w._terminate_tree(api, 11, 12, hj, hp)
    assert ok is False and err == INVALID and hj.quarantined and not hp.quarantined
    assert jc == [11] and pc == [12]            # job never retried; process fallback still tried
    w._terminate_tree(api, 11, 12, hj, hp)
    assert jc == [11], 'quarantined job number is never terminated again'


def test_r8_terminate_process_error_6_quarantines_process_in_helper():
    pc = []; api = _failing_api('TerminateProcess', INVALID, pc)
    hp = w._Handle(api, 12, 'process')
    assert w._terminate_process(api, 12, hp) == (False, INVALID) and hp.quarantined


# ---- whole run, real classifiers over a fake kernel ----
class Hybrid(Fake):
    """Fake for everything except the classified methods, which run the REAL WinApi code over
    stub kernel functions. fail[(kernel_fn, handle)] = GetLastError code => FALSE with that error."""
    def __init__(self, *a, fail=None, **kw):
        super().__init__(*a, **kw); self.fail = dict(fail or {}); self.kcalls = []; self.lasterr = 0
        sup = lambda f: (lambda h, *r: self._k(f, h, *r))
        self.real = _bare_api(
            TerminateJobObject=lambda h, c: self._k('TerminateJobObject', h, lambda: Fake.terminate_job(self, h)),
            TerminateProcess=lambda h, c: self._k('TerminateProcess', h, lambda: Fake.terminate_process(self, h)),
            CancelIoEx=lambda h, o: self._k('CancelIoEx', h, lambda: Fake.cancel_io(self, h)),
            CancelSynchronousIo=lambda h: self._k('CancelSynchronousIo', h, lambda: Fake.cancel_sync_io(self, h)),
            AssignProcessToJobObject=lambda j, p: self._k('AssignProcessToJobObject', j, lambda: Fake.assign(self, j, p) or True)
            if (self.fail.get(('AssignProcessToJobObject', p)) is None) else self._k('AssignProcessToJobObject', p, None),
            ResumeThread=lambda t: self._kr('ResumeThread', t),
            SetHandleInformation=lambda h, m, f: self._k('SetHandleInformation', h, lambda: True))
        self.real.err = lambda: self.lasterr
    def _k(self, name, h, ok):
        self.kcalls.append((name, h)); e = self.fail.get((name, h))
        if e is not None: self.lasterr = e; return 0
        return 1 if ok() else 0
    def _kr(self, name, h):
        self.kcalls.append((name, h)); e = self.fail.get((name, h))
        if e is not None: self.lasterr = e; return 0xFFFFFFFF
        self._log('resume'); return 1
    def kn(self, name, h): return self.kcalls.count((name, h))
    def terminate_job(self, j): return self.real.terminate_job(j)
    def terminate_process(self, p): return self.real.terminate_process(p)
    def cancel_io(self, h): return self.real.cancel_io(h)
    def cancel_sync_io(self, h): return self.real.cancel_sync_io(h)
    def assign(self, j, p): return self.real.assign(j, p)
    def resume(self, t): return self.real.resume(t)
    def uninheritable(self, h): self.real.uninheritable(h)


def test_r8_run_terminate_job_error_6_quarantines_job_and_it_is_never_touched_again():
    f = Hybrid(fail={('TerminateJobObject', JOB): INVALID})
    with pytest.raises(w.IsolationError): run(f)
    assert w.retained_count() == 1 and 'job' in w._RETAINED[0].quarantined
    f.fail.clear()
    for _ in range(4): w.reap_retained()
    assert f.kn('TerminateJobObject', JOB) == 1 and f.closed.count(JOB) == 0 and w.retained_count() == 1


def test_r8_run_terminate_process_error_6_in_reaper_quarantines_process(monkeypatch):
    f = Hybrid(assign_fails=True, fail={('AssignProcessToJobObject', PROC): DENIED, ('TerminateProcess', PROC): INVALID})
    with pytest.raises(w.IsolationError): run(f)
    assert f.kn('TerminateProcess', PROC) == 1 and 'process' in w._RETAINED[0].quarantined
    f.fail.clear()
    for _ in range(4): w.reap_retained()
    assert f.kn('TerminateProcess', PROC) == 1 and f.closed.count(PROC) == 0


def test_r8_run_assign_error_6_quarantines_job_and_process_and_uses_neither():
    f = Hybrid(fail={('AssignProcessToJobObject', PROC): INVALID})
    with pytest.raises(w.IsolationError, match='exit could not be confirmed'): run(f)
    assert f.kn('TerminateProcess', PROC) == 0 and f.kn('TerminateJobObject', JOB) == 0
    assert 'resume' not in f.calls
    q = w._RETAINED[0].quarantined
    assert 'job' in q and 'process' in q
    f.fail.clear()
    for _ in range(3): w.reap_retained()
    assert f.closed.count(PROC) == 0 and f.closed.count(JOB) == 0 and w.retained_count() == 1


def test_r8_run_resume_error_6_quarantines_primary_thread_never_closed():
    f = Hybrid(fail={('ResumeThread', THR): INVALID})
    with pytest.raises(w.IsolationError): run(f)
    assert 'primary-thread' in w._RETAINED[0].quarantined
    for _ in range(3): w.reap_retained()
    assert f.closed.count(THR) == 0


@pytest.mark.parametrize('hid', [IN_W, OUT_R])
def test_r8_run_uninheritable_error_6_quarantines_that_parent_end(hid):
    f = Hybrid(fail={('SetHandleInformation', hid): INVALID})
    with pytest.raises(w.IsolationError): run(f)
    assert w.retained_count() == 1 and f.closed.count(hid) == 0
    assert len(w._RETAINED[0].quarantined) >= 1


def _reader_handle(f):
    return [h for h, n in f.thread_handles.items() if n == 'job-stdout-reader'][0]


def test_r8_run_cancel_sync_error_6_quarantines_thread_slot_and_is_called_once(monkeypatch):
    monkeypatch.setattr(w, 'JOIN_WAIT_SECONDS', 1.0)
    f = Hybrid(behavior='stuck_forever', fail={('CancelSynchronousIo', 7000): INVALID, ('CancelSynchronousIo', 7001): INVALID})
    try:
        with pytest.raises(w.IsolationError): run(f, wall=0.1)
        rh = _reader_handle(f)
        assert f.kn('CancelSynchronousIo', rh) == 1, 'invalid thread handle cancelled exactly once'
        assert w._RETAINED and w._RETAINED[0].quarantined.count('io-thread-handle') >= 1
    finally:
        f.release.set()


def test_r8_run_cancel_sync_1168_is_ordinary_retried_never_quarantined(monkeypatch):
    monkeypatch.setattr(w, 'JOIN_WAIT_SECONDS', 2.0)
    class Slow(Hybrid):
        def _k(self, name, h, ok):
            if name == 'CancelSynchronousIo' and self.kn(name, h) < 2:
                self.kcalls.append((name, h)); self.lasterr = NOT_FOUND; return 0
            return super()._k(name, h, ok)
    f = Slow(behavior='stuck_reader', fail={('CancelIoEx', OUT_R): NOT_FOUND, ('CancelIoEx', IN_W): NOT_FOUND})
    with pytest.raises(w.IsolationError): run(f, wall=0.1)
    assert f.kn('CancelSynchronousIo', _reader_handle(f)) >= 3, 'two 1168 results, then the cancel that works'
    assert not any(e.quarantined for e in w._RETAINED)
    assert w.retained_count() == 0


def test_r8_run_cancel_io_fallback_error_6_quarantines_pipe_end(monkeypatch):
    monkeypatch.setattr(w, 'JOIN_WAIT_SECONDS', 1.0)
    f = Hybrid(behavior='stuck_forever', fail={('CancelIoEx', OUT_R): INVALID, ('CancelIoEx', IN_W): INVALID,
                                                ('CancelSynchronousIo', 7000): NOT_FOUND, ('CancelSynchronousIo', 7001): NOT_FOUND})
    try:
        with pytest.raises(w.IsolationError): run(f, wall=0.1)
        assert f.kn('CancelIoEx', OUT_R) == 1, 'invalid pipe-end identity cancelled exactly once'
        assert 'stdout-parent-end' in w._RETAINED[0].quarantined
    finally:
        f.release.set()


# =====================================================================
# REV 9: post-allocation ownership. Everything below goes THROUGH run_in_job with the REAL
# WinApi.create_job/configure_job/close adapter code over stub kernel functions (a fake kernel),
# and asserts RETENTION and CAPACITY ACCOUNTING, not just "was close called".
# Linux only, no Windows. These prove orchestration/accounting against a fake; not kernel behaviour.
# =====================================================================
class KFake(Fake):
    """Job allocation, limits and every CloseHandle go through the real WinApi adapter methods over
    stub kernel functions. setinfo_err: GetLastError when SetInformationJobObject fails (None = ok).
    close_ok: CloseHandle result. Counts raw kernel allocations and closes per number."""
    def __init__(self, *a, setinfo_err=None, close_ok=True, **kw):
        super().__init__(*a, **kw)
        self.k_alloc = 0; self.k_close = []; self.setinfo_err = setinfo_err; self.close_ok = close_ok
        self.lasterr = 0
        def create(*x): self.k_alloc += 1; return self._h()
        def setinfo(*x):
            if self.setinfo_err is None: return 1
            self.lasterr = self.setinfo_err; return 0
        def closeh(h): self.k_close.append(h); return 1 if self.close_ok else 0
        self.real = _bare_api(CreateJobObjectW=create, SetInformationJobObject=setinfo, CloseHandle=closeh)
        self.real.err = lambda: self.lasterr
    def create_job(self): self._log('job'); self.job_handle = self.real.create_job(); return self.job_handle
    def configure_job(self, job, limits): self.limits = limits; self.real.configure_job(job, limits)
    def close(self, h):
        self._log('close:%d' % h); self.closed.append(h)
        if h == self.job_handle: self.job_closed = True
        return self.real.close(h)


@pytest.mark.parametrize('err', [INVALID, DENIED])
def test_r9_setinfo_failure_close_false_retains_and_counts_against_capacity_writer_repro(monkeypatch, err):
    """Writer's repro: six runs, cap 4. Error 6: one allocation per run, ZERO closes, every job quarantined and
    retained; error 5 + CloseHandle FALSE: exactly one close attempt per run (owner), quarantined, retained.
    Either way the fifth run is REFUSED by admission, and no reservation leaks."""
    monkeypatch.setattr(w, 'MAX_RETAINED', 4)
    f = KFake(setinfo_err=err, close_ok=False); refused = 0
    for i in range(6):
        with pytest.raises(w.IsolationError) as ei: run(f)
        if 'Too many unreaped' in str(ei.value): refused += 1
    assert (f.k_alloc, refused) == (4, 2)                          # runs 5 and 6 refused before allocating
    assert w.retained_count() == 4 and w.active_count() == 0
    assert all(e.job.quarantined and e.job.is_open and e.proc is not None for e in w._RETAINED)
    assert all(e.proc.h is None and e.thread.h is None for e in w._RETAINED)        # nothing else allocated
    assert len(f.k_close) == (0 if err == INVALID else 4)         # never closes an invalid number; else once each
    assert len(set(f.k_close)) == len(f.k_close)
    n = len(f.k_close); w.reap_retained()
    assert len(f.k_close) == n and w.retained_count() == 4         # quarantined identity is never retried or freed
    assert not [c for c in f.calls if c.startswith(('terminate', 'cancel', 'assign', 'resume'))]


def test_r9_setinfo_failure_close_ok_closes_exactly_once_and_leaves_nothing_retained():
    f = KFake(setinfo_err=DENIED, close_ok=True)
    for _ in range(3):
        with pytest.raises(w.IsolationError, match='SetInformationJobObject failed'): run(f)
    assert w.retained_count() == 0 and w.active_count() == 0
    assert len(f.k_close) == f.k_alloc == 3 and len(set(f.k_close)) == 3
    assert 'create_suspended' not in f.calls


STEPS = ['create_job', 'configure_job', 'create_pipe#1', 'create_pipe#2', 'open_nul', 'uninheritable#1',
         'uninheritable#2', 'create_suspended', 'assign', 'resume']


class FailAt(Fake):
    """Raises (IsolationError, or InvalidHandleError) at the named step; every close result is configurable."""
    def __init__(self, step, exc=None, close_ok=True, **kw):
        super().__init__(**kw); self.step = step; self.exc = exc or w.IsolationError('boom')
        self.close_ok = close_ok; self.cnt = {}; self.k_alloc = []
    def _hit(self, name):
        self.cnt[name] = self.cnt.get(name, 0) + 1
        key = name if name in ('create_job', 'configure_job', 'open_nul', 'create_suspended', 'assign', 'resume') else '%s#%d' % (name, self.cnt[name])
        if key == self.step: raise self.exc
    def create_job(self): self._hit('create_job'); h = super().create_job(); self.k_alloc.append(h); return h
    def configure_job(self, j, l): self._hit('configure_job')
    def create_pipe(self):
        self._hit('create_pipe'); r, wr = super().create_pipe(); self.k_alloc += [r, wr]; return r, wr
    def open_nul(self): self._hit('open_nul'); h = super().open_nul(); self.k_alloc.append(h); return h
    def uninheritable(self, h): self._hit('uninheritable')
    def create_suspended(self, *a):
        self._hit('create_suspended'); p, t = super().create_suspended(*a); self.k_alloc += [p, t]; return p, t
    def assign(self, j, p): self._hit('assign')
    def resume(self, t): self._hit('resume')
    def close(self, h): super().close(h); return self.close_ok


@pytest.mark.parametrize('step', STEPS)
def test_r9_every_setup_failure_closes_each_allocation_once_when_close_works(step):
    f = FailAt(step)
    with pytest.raises(w.IsolationError): run(f)
    assert w.retained_count() == 0 and w.active_count() == 0
    assert sorted(f.closed) == sorted(f.k_alloc) and len(set(f.closed)) == len(f.closed)   # each exactly once


@pytest.mark.parametrize('step', STEPS)
def test_r9_every_setup_failure_with_close_false_retains_each_allocation_once_and_counts_capacity(step, monkeypatch):
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.05); monkeypatch.setattr(w, 'REAP_WAIT_SECONDS', 0.05)
    f = FailAt(step, close_ok=False)
    with pytest.raises(w.IsolationError): run(f)
    assert w.active_count() == 0                                   # reservation converted, not leaked or doubled
    if not f.k_alloc:
        assert w.retained_count() == 0; return
    assert w.retained_count() == 1
    e = w._RETAINED[0]
    held = {h.h for h in (e.thread, e.proc, e.job, *e.io_handles) if h is not None and h.h} | {s.h for s in e.slots if s.h}
    assert held == set(f.k_alloc), 'every allocated number is still owned: %s vs %s' % (held, f.k_alloc)
    assert len(set(f.closed)) == len(f.closed), 'a failed close is never retried (identity quarantine)'


@pytest.mark.parametrize('step', ['create_job', 'configure_job', 'create_pipe#1', 'create_pipe#2', 'open_nul',
                                  'create_suspended', 'assign', 'resume'])
def test_r9_cap_is_never_exceeded_by_repeated_allocation_then_failure(step, monkeypatch):
    monkeypatch.setattr(w, 'MAX_RETAINED', 3); monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.02)
    monkeypatch.setattr(w, 'REAP_WAIT_SECONDS', 0.02)
    f = FailAt(step, close_ok=False); first_allocs = None
    for i in range(6):
        with pytest.raises(w.IsolationError): run(f)
        assert w.retained_count() + w.active_count() <= 3
        if w.retained_count() == 3 and first_allocs is None: first_allocs = len(f.k_alloc)
    if f.k_alloc:
        assert w.retained_count() == 3 and len(f.k_alloc) == first_allocs, 'no allocation after the cap is reached'


def test_r9_create_job_allocation_failure_has_nothing_to_own():
    f = FailAt('create_job')
    with pytest.raises(w.IsolationError): run(f)
    assert f.closed == [] and w.retained_count() == 0 and w.active_count() == 0


def test_r9_invalid_handle_on_configure_quarantines_job_through_owner():
    f = FailAt('configure_job', exc=w.InvalidHandleError('invalid'))
    with pytest.raises(w.IsolationError): run(f)
    assert f.job_handle not in f.closed
    assert w.retained_count() == 1 and w._RETAINED[0].quarantined == ['job'] and w.active_count() == 0


def test_r9_cleanup_that_raises_retains_everything_open_and_converts_reservation(monkeypatch):
    monkeypatch.setattr(w, 'KILL_WAIT_SECONDS', 0.05); monkeypatch.setattr(w, 'REAP_WAIT_SECONDS', 0.05)
    def boom(*a, **k): raise RuntimeError('cleanup bug')
    monkeypatch.setattr(w, '_stop_io_threads', boom)
    f = Fake()
    with pytest.raises(RuntimeError): run(f)
    assert w.active_count() == 0 and w.retained_count() == 1
    e = w._RETAINED[0]
    assert e.proc.is_open and e.job.is_open and e.thread.is_open and not e.exit_ok and not e.term_ok
    assert not any(c in f.closed for c in (f.proc, f.thr, f.job_handle)), 'controls not closed while death unproven'
    monkeypatch.undo(); w._RETAINED[0].threads[:] = []           # reaper can later finish it
    for _ in range(3):
        if w.reap_retained() == 0: break
    assert w.retained_count() == 0 and w.active_count() == 0 and 'terminate_job' in f.calls


def test_r9_owner_exists_before_allocation_and_adopts_once():
    h = w._Handle(Fake(), None, 'job')
    assert not h.is_open and h.close() is True and h.value() is None
    h.adopt(5)
    with pytest.raises(w.IsolationError): h.adopt(6)
    assert h.value() == 5


def test_r9_create_suspended_adapter_returns_both_handles_even_if_attribute_list_delete_raises():
    def init(buf, n, flags, size):
        if buf is None: size._obj.value = 64; return 0
        return 1
    def cp(app, cmd, pa, ta, inh, flags, env, cwd, si, pi):
        pi._obj.hProcess = 111; pi._obj.hThread = 222; return 1
    def delete(buf): raise OSError('delete failed')
    a = _bare_api(InitializeProcThreadAttributeList=init, UpdateProcThreadAttribute=lambda *x: 1,
                  CreateProcessW=cp, DeleteProcThreadAttributeList=delete)
    assert a.create_suspended(['x.exe'], b'\0\0', '.', 1, 2, 3) == (111, 222)


def test_r9_pid_state_failed_close_is_quarantined_counted_and_never_retried():
    closes = []
    a = _bare_api(OpenProcess=lambda *x: 55, CloseHandle=lambda h: closes.append(h) or 0,
                  GetExitCodeProcess=lambda h, c: (setattr(c._obj, 'value', w.STILL_ACTIVE), 1)[1])
    assert w.pid_state(1234, a) == w.PID_ALIVE
    assert closes == [55] and w.retained_count() == 1 and w._RETAINED[0].quarantined == ['diagnostic-process']
    w.reap_retained(); assert closes == [55]


def test_r9_writer_and_reader_use_owner_values_not_raw_numbers():
    f = Rec(behavior='echo'); out = run(f)
    assert out == b'in'
    f2 = Rec(behavior='echo')
    # stdin parent end quarantined by error 6 on uninheritable: the writer thread is never started, nothing writes
    f2.uninheritable = lambda h: (_ for _ in ()).throw(w.InvalidHandleError('invalid'))
    with pytest.raises(w.IsolationError): run(f2)
    assert not f2.sent and w.retained_count() == 1
