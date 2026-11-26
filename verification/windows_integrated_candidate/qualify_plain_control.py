"""Diagnostic-only unconfined control run of the staged candidate worker. Not acceptance, not product."""
import os
import subprocess
import tempfile
import time


OUTPUT_CAP = 4_000_000


def bounded_run(command, request, environment, cwd, timeout, cap=OUTPUT_CAP):
    """Run command with stdin/stdout on temp files (no pipes, no deadlock) and a SOFT output threshold.

    Output size is polled about every 20 ms, so the file on disk may overshoot cap before the kill. Only the
    RETURNED memory is hard-bounded (at most cap+1 bytes). Past cap the child is killed and reaped and cap+1
    bytes are returned (the caller sees "over cap"). Past timeout the child is killed and reaped and
    TimeoutExpired is raised. Worker stderr is discarded like the launcher's NUL.
    """
    with tempfile.TemporaryFile() as stdin, tempfile.TemporaryFile() as output:
        stdin.write(request)
        stdin.seek(0)
        child = subprocess.Popen(command, stdin=stdin, stdout=output, stderr=subprocess.DEVNULL,
                                 env=environment, cwd=cwd)
        deadline = time.monotonic() + timeout
        try:
            while child.poll() is None:
                if os.fstat(output.fileno()).st_size > cap:
                    break
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired('plain', timeout)
                time.sleep(0.02)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
        output.seek(0)
        data = output.read(cap + 1)
        return child.returncode, data


def plain_line(label, run_plain, decoder):
    """qual_plain: ONE unconfined run of the same staged python/worker with the same stdin and env.

    Diagnostic only. No AppContainer, no Job, no debugger, no ACL change. It reports the child exit
    code (hex), stdout size and whether the strict worker decoder accepted the output. Fixed vocabulary:
    never stderr, paths or pids. It cannot establish sandbox acceptance and never changes the exit code.
    A plain success implicates only the restricted/debugged launch context (several differences at once);
    a plain failure does not show the sandbox is irrelevant.
    """
    try:
        returncode, output = run_plain()
    except subprocess.TimeoutExpired:
        return 'qual_plain label=%s exit=timeout out_bytes=0 decode=na' % label
    except Exception:
        return 'qual_plain label=%s exit=none out_bytes=0 decode=na' % label
    code = '%08x' % (returncode & 0xFFFFFFFF)
    if returncode != 0 or len(output) > OUTPUT_CAP:
        return 'qual_plain label=%s exit=%s out_bytes=%d decode=na' % (label, code, len(output))
    try:
        decoder(output)
        decode = 'ok'
    except Exception:
        decode = 'refused'
    return 'qual_plain label=%s exit=%s out_bytes=%d decode=%s' % (label, code, len(output), decode)


def guarded_plain_control(*args):
    """Never raises: any harness-side failure becomes a fixed line, so the main report and exit code survive."""
    try:
        return plain_control(*args)
    except Exception:
        return 'qual_plain label=none exit=none out_bytes=0 decode=na error_class=harness'


def plain_control(candidate_parent, stage, manifest_pin, equivalence_payloads, request_bytes, ALIASES):
    from app.fingerprint_bounded_parse import _decode_worker
    label, filename, raw = equivalence_payloads()[0]
    request = request_bytes(raw, filename)
    environment = {'LOCALAPPDATA': os.environ['LOCALAPPDATA'], 'SYSTEMROOT': os.environ['SYSTEMROOT']}

    def run_plain():
        with candidate_parent.held_tree(stage, manifest_pin) as held:
            runtime = held / 'runtime'
            return bounded_run([str(runtime / 'python.exe'), '-I', '-B', '-X', 'utf8', str(runtime / 'candidate_worker.py')],
                               request, environment, str(runtime), 3)
    return plain_line(label, run_plain, lambda output: _decode_worker(output, ALIASES))
