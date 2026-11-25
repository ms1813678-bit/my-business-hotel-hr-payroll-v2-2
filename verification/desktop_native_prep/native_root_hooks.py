"""NEW hidden native root-hook smoke, disposable CI only, no remote navigation.

A real hidden WinForms/WebView2 control is initialized. This does not test visible
UI, authenticated PDF, print/download, network isolation, or production fatal UI.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
import uuid


def write_result(path, **fields):
    Path(path).write_text(json.dumps({'native_app_acceptance': False, **fields}, sort_keys=True)+'\n')


def child(guard_path, profile, result):
    import webview
    spec = importlib.util.spec_from_file_location('hony_exact_guard', guard_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    class HeadlessGuard(module.WebView2OriginGuard):
        def _fatal_shutdown(self):
            # Test seam only. Production fatal UI must be tested interactively.
            write_result(result, status='guard-fatal', reason=self._failure,
                         production_fatal_ui_tested=False)
            os._exit(1)

    webview.settings['IGNORE_SSL_ERRORS'] = False
    webview.settings['ALLOW_DOWNLOADS'] = True
    window = webview.create_window('HOnY hidden native qualification', None,
                                  html=None, hidden=True, width=400, height=300)
    guard = HeadlessGuard('https://hony-native-qualification.invalid', install_timeout=20)
    outcome = {}

    def qualify():
        try:
            guard.install(window)
            guard.assert_ready()
            # Do not call guard.start/load_start_url/window.load_url. An initialized
            # placeholder is not app content or a TLS origin test.
            def inspect_and_dispose():
                core = guard._control.CoreWebView2
                outcome.update(status='root-hooks-ready', renderer=webview.renderer,
                               browser_version=str(core.Environment.BrowserVersionString),
                               guard_ready=True, allowed_navigations=guard.allowed_navigations,
                               blocked_navigations=guard.blocked_navigations,
                               remote_start_called=False, hidden=True,
                               production_fatal_ui_tested=False)
                guard._control.Dispose()
                outcome['control_disposed'] = bool(guard._control.IsDisposed)
            guard._marshal_to_ui(guard._control, inspect_and_dispose)
            if not outcome.get('control_disposed'):
                raise RuntimeError('dispose-not-confirmed')
            window.destroy()
        except BaseException as exc:
            write_result(result, status='smoke-failed', exception_type=type(exc).__name__,
                         production_fatal_ui_tested=False)
            os._exit(1)

    webview.start(func=qualify, gui='edgechromium', debug=False,
                  private_mode=False, storage_path=profile)
    if not outcome.get('guard_ready') or not outcome.get('control_disposed'):
        write_result(result, status='unconfirmed-shutdown')
        return 1
    write_result(result, **outcome, message_loop_returned=True,
                 guard_source_sha256=hashlib.sha256(Path(guard_path).read_bytes()).hexdigest())
    return 0


def parent(guard_path, work):
    # The exact child is created suspended, assigned to an owned kill-on-close
    # Job before its single resume. No token policy/sandbox guarantee is claimed.
    import subprocess
    import win32api
    import win32con
    import win32event
    import win32job
    import win32process
    import pywintypes
    import win32file
    work = Path(work)
    if work.exists():
        raise RuntimeError('fresh-work-root-required')
    work.mkdir()
    profile = work/'profile'
    result = work/'result.json'
    output = work/'child-output.log'
    # pywin32's binding requires a string name. Refuse a pre-existing object
    # before changing limits or assigning any process.
    win32api.SetLastError(0)
    job = win32job.CreateJobObject(None, 'Local\\HOnY-qualification-' + uuid.uuid4().hex)
    if win32api.GetLastError() == 183:
        job.Close()
        raise RuntimeError('refusing-pre-existing-job-object')
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    sa = pywintypes.SECURITY_ATTRIBUTES()
    sa.bInheritHandle = True
    log = win32file.CreateFile(str(output), win32con.GENERIC_WRITE,
                             win32con.FILE_SHARE_READ, sa, win32con.CREATE_NEW, 0, None)
    null = win32file.CreateFile('NUL', win32con.GENERIC_READ,
                              win32con.FILE_SHARE_READ|win32con.FILE_SHARE_WRITE,
                              sa, win32con.OPEN_EXISTING, 0, None)
    startup = win32process.STARTUPINFO()
    startup.dwFlags |= win32con.STARTF_USESTDHANDLES
    startup.hStdOutput = log
    startup.hStdError = log
    startup.hStdInput = null
    process = thread = None
    try:
        command = subprocess.list2cmdline([sys.executable, '-I', str(Path(__file__).resolve()),
                                         '--child', str(Path(guard_path).resolve()), str(profile), str(result)])
        process, thread, pid, tid = win32process.CreateProcess(
            sys.executable, command, None, None, True, win32con.CREATE_SUSPENDED,
            None, str(work), startup)
        win32job.AssignProcessToJobObject(job, process)
        if win32process.ResumeThread(thread) != 1:
            raise RuntimeError('unexpected-prior-suspend-count')
        wait = win32event.WaitForSingleObject(process, 60000)
        if wait != win32con.WAIT_OBJECT_0:
            raise RuntimeError('bounded-native-smoke-timeout')
        exit_code = win32process.GetExitCodeProcess(process)
        if result.is_file():
            data = json.loads(result.read_text())
        else:
            data = {'status':'no-result', 'native_app_acceptance':False}
        data['child_exit_code'] = exit_code
        print(json.dumps(data, sort_keys=True))
        return 0 if exit_code == 0 and data.get('status') == 'root-hooks-ready' else 1
    finally:
        win32job.TerminateJobObject(job, 99)
        if process is not None:
            # Assignment can fail while the child is still suspended and outside
            # the Job. Explicitly terminate our exact owned process handle too.
            if win32event.WaitForSingleObject(process, 0) != win32con.WAIT_OBJECT_0:
                win32process.TerminateProcess(process, 99)
            if win32event.WaitForSingleObject(process, 5000) != win32con.WAIT_OBJECT_0:
                raise RuntimeError('owned-child-cleanup-unconfirmed')
        deadline = time.monotonic() + 5
        while win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)['ActiveProcesses']:
            if time.monotonic() >= deadline:
                raise RuntimeError('owned-job-descendant-cleanup-unconfirmed')
            time.sleep(0.05)
        print('NATIVE_HOOK_OWNED_JOB_CLEANUP_PASS')
        for handle in (thread, process, null, log, job):
            if handle is not None:
                handle.Close()


if __name__ == '__main__':
    if sys.platform != 'win32':
        raise SystemExit('Windows-only qualification')
    if len(sys.argv) == 5 and sys.argv[1] == '--child':
        raise SystemExit(child(*sys.argv[2:]))
    if len(sys.argv) != 3:
        raise SystemExit('usage: native_root_hooks.py guard.py NEW_WORK_ROOT')
    raise SystemExit(parent(*sys.argv[1:]))
