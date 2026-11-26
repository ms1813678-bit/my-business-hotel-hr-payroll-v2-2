"""Text-only reader for a tracerpt dump of one child. Prints a bounded set of raw lines. Never writes, never loads."""
import argparse
import re
import sys
import time

MAX_BYTES = 400_000_000
MAX_SECONDS = 180
MAX_OUT_LINES = 300
LINE_CUT = 400
TAIL_IMAGES = 40
TRUNC_BYTES = 63 * 1024 * 1024

DENIED = re.compile(r'0xC0000022|STATUS_ACCESS_DENIED|ACCESS_DENIED', re.I)
PID_KEYS = r'(?:ProcessId|Process ?Id|PID|ProcessID)'
PID_VALUE = re.compile(r'(?<![A-Za-z])' + PID_KEYS + r'''['"]?\s*[=:>]\s*['"]?(0x[0-9A-Fa-f]+|[0-9]+)''', re.I)
PID_XML = re.compile(r'Name=[\'"]' + PID_KEYS + r'[\'"][^>]*>\s*(0x[0-9A-Fa-f]+|[0-9]+)\s*<', re.I)
IRP = re.compile(r'(?:Irp(?:Ptr)?|IrpPtr)[\'"]?\s*[=:>]\s*[\'"]?(0x[0-9A-Fa-f]+)|Name=[\'"]Irp(?:Ptr)?[\'"][^>]*>\s*(0x[0-9A-Fa-f]+)\s*<', re.I)
START = re.compile(r'(?<![A-Za-z])(?:Process[_ ,]*(?:V\d+_)?Start|Start)(?![A-Za-z])|Opcode[\'"]?\s*[=:>]\s*[\'"]?1(?![0-9])', re.I)
EXIT_STATUS = re.compile(r'ExitStatus[\'"]?\s*[=:>]\s*[\'"]?(0x[0-9A-Fa-f]+|[0-9]+)|Name=[\'"]ExitStatus[\'"][^>]*>\s*(0x[0-9A-Fa-f]+|[0-9]+)\s*<', re.I)


class Refused(Exception):
    pass


def records(text):
    """One record per XML <Event>...</Event> block (joined to one line), otherwise one per non-empty line."""
    if '<Event' in text:
        parts = re.findall(r'<Event\b.*?</Event>', text, flags=re.S)
        return [' '.join(p.split()) for p in parts]
    return [l for l in text.splitlines() if l.strip()]


def pid_of(record):
    m = PID_XML.search(record) or PID_VALUE.search(record)
    return int(m.group(1), 0) if m else None


def has_pid(record, pid):
    return any(int(v, 0) == pid for v in PID_XML.findall(record) + PID_VALUE.findall(record))


def pick_pids(recs, token):
    token = token.lower()
    pids = []
    for r in recs:
        low = r.lower()
        if token in low and 'python.exe' in low and START.search(r) and 'dcstart' not in low:
            pid = pid_of(r)
            if pid is not None and pid not in pids:
                pids.append(pid)
    return pids


def cut(line):
    return line[:LINE_CUT]


def analyze(text, token, truncated=False, deadline=None):
    out = []
    recs = records(text)
    pids = pick_pids(recs, token)
    pid_recs = []
    denied = []
    for i, r in enumerate(recs):
        if deadline is not None and time.monotonic() > deadline:
            raise Refused('analysis time cap reached')
        if any(has_pid(r, p) for p in pids):
            pid_recs.append((i, r))
            if DENIED.search(r) and re.search(r'file|regist|reg|create|open|query|irp', r, re.I):
                denied.append((i, r))
    out.append('trace_counts total_events=%d child_pids=%s pid_events=%d denied_events=%d trace_truncated=%s' % (
        len(recs), ','.join(str(p) for p in pids) or 'none', len(pid_recs), len(denied), 'yes' if truncated else 'no'))
    if not pids:
        out.append('trace_reading 3_no_child_pid_or_session_empty=inconclusive no_rerun')
    shown = 0
    for i, r in denied:
        if len(out) >= MAX_OUT_LINES - TAIL_IMAGES - 8 or shown >= 60:
            out.append('trace_denied_output_capped=1')
            break
        m = IRP.search(r)
        irp = (m.group(1) or m.group(2)) if m else None
        if irp:
            for j in range(i - 1, -1, -1):
                prev = recs[j]
                if irp.lower() in prev.lower() and re.search(r'create|open', prev, re.I) and not DENIED.search(prev):
                    out.append('trace_open ' + cut(prev))
                    break
        out.append('trace_denied ' + cut(r))
        shown += 1
    images = [r for _i, r in pid_recs if re.search(r'image|img', r, re.I) and re.search(r'load', r, re.I)]
    for r in images[-TAIL_IMAGES:]:
        out.append('trace_image_load ' + cut(r))
    stops = [r for _i, r in pid_recs if re.search(r'(?<![A-Za-z])(?:End|Stop|Exit)', r) and EXIT_STATUS.search(r) and re.search(r'process', r, re.I)]
    for r in stops[-3:]:
        out.append('trace_process_stop ' + cut(r))
    if pids:
        if denied:
            out.append('trace_reading 1_denied_open_with_object=named_object_only;fix_is_a_separate_design_needing_review_and_owner_consent')
        else:
            out.append('trace_reading 2_no_denied_file_or_registry_open=denial_not_a_file_or_registry_open;last_images_show_loader_progress;instrument_exhausted_report_to_owner')
    out.append('trace_limits diagnostic_only=1 negative_inconclusive=1 security_acceptance=NOT_ESTABLISHED no_cause_claimed_beyond_a_named_object=1 no_rerun=1 no_environment_values_printed=1')
    return out[:MAX_OUT_LINES]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--dump', required=True)
    p.add_argument('--token', required=True)
    p.add_argument('--etl-bytes', type=int, default=0)
    a = p.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9._-]{8,80}', a.token):
        print('trace_analysis_refused reason=bad_token')
        return 2
    try:
        with open(a.dump, 'rb') as f:
            data = f.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise Refused('dump over size cap')
        text = data.decode('utf-8', errors='replace')
        lines = analyze(text, a.token, truncated=a.etl_bytes >= TRUNC_BYTES, deadline=time.monotonic() + MAX_SECONDS)
    except (Refused, OSError) as exc:
        print('trace_analysis_refused reason=' + str(exc)[:60] + ' inconclusive=1 no_rerun=1')
        return 2
    for line in lines:
        print(line)
    return 0


if __name__ == '__main__':
    sys.exit(main())
