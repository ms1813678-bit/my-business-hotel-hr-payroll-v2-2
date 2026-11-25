"""Diagnostic-only trusted-side driver for the same-token loader isolation child. Never acceptance.

Fails closed: a nonzero exit unless the launcher exited 0 and the child printed exactly one
leading `probe_start ok=1` and one trailing `probe_done ok=1`, with no rejected or truncated line.
Only fixed-vocabulary lines are ever printed; everything else is counted, not shown."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

LIMIT = 16384
PAIRS = r'( [A-Za-z0-9_]+=[0-9A-Za-z_.-]+)*$'
CHILD_LINE = re.compile(r'^probe_[a-z0-9_]+' + PAIRS)
NATIVE_LINE = re.compile(r'^(CANDIDATE_(CREATED|RESUMED|REFUSAL|CLEANUP)|loader_debug_[a-z_]+|startup_process_exit'
                         r'|(HELD_)?DEBUG_CLEANUP_UNCONFIRMED|READER_CLEANUP_UNCONFIRMED|FEEDER_CLEANUP_UNCONFIRMED)' + PAIRS)


def evaluate(returncode, stdout, stderr):
    """Return (exit_code, printable_lines). Pure function, no side effects."""
    lines = []
    reason = None
    truncated = len(stdout) > LIMIT or len(stderr) > LIMIT
    child = stdout[:LIMIT].decode('ascii', errors='replace').splitlines()
    native = stderr[:LIMIT].decode('ascii', errors='replace').splitlines()
    lines.append('probe_launcher_exit code=' + str(returncode))
    accepted = []
    rejected = 0
    for line in child:
        if CHILD_LINE.match(line):
            accepted.append(line)
        else:
            rejected += 1
    lines.extend(accepted)
    lines.append('probe_child_lines count=%d rejected=%d' % (len(child), rejected))
    shown = 0
    dropped = 0
    for line in native:
        if NATIVE_LINE.match(line):
            lines.append(line)
            shown += 1
        else:
            dropped += 1
    lines.append('probe_native_lines shown=%d dropped=%d' % (shown, dropped))
    if returncode != 0:
        reason = 'launcher_nonzero'
    elif truncated:
        reason = 'truncated'
    elif rejected:
        reason = 'rejected_line'
    elif not accepted or accepted[0] != 'probe_start ok=1' or accepted.count('probe_start ok=1') != 1:
        reason = 'missing_start'
    elif accepted[-1] != 'probe_done ok=1' or accepted.count('probe_done ok=1') != 1:
        reason = 'missing_done'
    if reason:
        lines.append('probe_driver status=refused reason=' + reason + ' security_acceptance=NOT_ESTABLISHED')
        return 1, lines
    lines.append('probe_driver status=complete security_acceptance=NOT_ESTABLISHED')
    return 0, lines


def main():
    from runtime_integrity import canonical, held_tree
    p = argparse.ArgumentParser()
    for name in ('launcher', 'profile', 'stage', 'manifest-pin', 'launcher-pin'):
        p.add_argument('--' + name, required=True)
    a = p.parse_args()
    launcher = canonical(a.launcher)
    with held_tree(a.stage, a.manifest_pin) as stage:
        runtime = stage / 'runtime'
        if hashlib.sha256(launcher.read_bytes()).hexdigest() != a.launcher_pin:
            raise ValueError('Pinned launcher mismatch')
        command = [str(launcher), a.profile, str(runtime / 'probe_load.exe'), str(runtime), str(runtime / 'python314.dll')]
        try:
            result = subprocess.run(command, input=b'{}', capture_output=True, timeout=10, check=False,
                                    env={'LOCALAPPDATA': os.environ['LOCALAPPDATA'], 'SYSTEMROOT': os.environ['SYSTEMROOT']},
                                    cwd=str(runtime))
        except subprocess.TimeoutExpired:
            print('probe_driver status=refused reason=timeout security_acceptance=NOT_ESTABLISHED')
            return 1
    code, lines = evaluate(result.returncode, result.stdout, result.stderr)
    for line in lines:
        print(line)
    return code


if __name__ == '__main__':
    sys.exit(main())
