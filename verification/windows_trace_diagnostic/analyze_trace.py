"""Streaming text-only reader for a tracerpt dump of one child. Prints a bounded set of raw lines.
Never writes, never loads, never reads the whole file: one <Event>...</Event> record at a time."""
import argparse
import codecs
import collections
import re
import sys
import time

MAX_BYTES = 2_000_000_000
MAX_SECONDS = 420
MAX_OUT_LINES = 300
LINE_CUT = 400
START_CUT = 300
TAIL_IMAGES = 40
TAIL_STOPS = 3
MAX_DENIED_SHOWN = 60
IRP_MAP_CAP = 20000
MAX_START_SAMPLES = 10
MAX_EVENT_NAMES = 200
CHUNK = 1 << 20
MAX_RECORD = 1 << 16
TRUNC_BYTES = 47 * 1024 * 1024

DENIED = re.compile(r'0xC0000022|3221225506|STATUS_ACCESS_DENIED|ACCESS_DENIED', re.I)
PID_KEYS = r'(?:ProcessId|Process ?Id|PID|ProcessID)'
PID_VALUE = re.compile(r'(?<![A-Za-z])' + PID_KEYS + r'''['"]?\s*[=:>]\s*['"]?(0x[0-9A-Fa-f]+|[0-9]+)''', re.I)
PID_XML = re.compile(r'Name=[\'"]' + PID_KEYS + r'[\'"][^>]*>\s*(0x[0-9A-Fa-f]+|[0-9]+)\s*<', re.I)
IRP = re.compile(r'(?:Irp(?:Ptr)?|IrpPtr)[\'"]?\s*[=:>]\s*[\'"]?(0x[0-9A-Fa-f]+)|Name=[\'"]Irp(?:Ptr)?[\'"][^>]*>\s*(0x[0-9A-Fa-f]+)\s*<', re.I)
START = re.compile(r'(?<![A-Za-z])(?:Process[_ ,]*(?:V\d+_)?Start|Start)(?![A-Za-z])|Opcode[\'"]?\s*[=:>]\s*[\'"]?1(?![0-9])', re.I)
NOT_START = re.compile(r'dcstart|dcend|defunct', re.I)
EXIT_STATUS = re.compile(r'ExitStatus[\'"]?\s*[=:>]\s*[\'"]?(0x[0-9A-Fa-f]+|[0-9]+)|Name=[\'"]ExitStatus[\'"][^>]*>\s*(0x[0-9A-Fa-f]+|[0-9]+)\s*<', re.I)
EVENT_OPEN = re.compile(r'<Event(?=[\s>])')
EVENT_NAME = re.compile(r'EventTypeName[\'"]?\s*[=:>]\s*[\'"]?([A-Za-z0-9_ ]{1,40})|<Opcode>([^<]{1,40})<|Opcode[\'"]?\s*=\s*[\'"]?([A-Za-z0-9_ ]{1,40})', re.I)
OPEN_LIKE = re.compile(r'create|open', re.I)
IMAGE_LOAD = re.compile(r'image|img', re.I)
LOAD = re.compile(r'load', re.I)
PROCESS_WORD = re.compile(r'process', re.I)
STOP_WORD = re.compile(r'(?<![A-Za-z])(?:End|Terminate|Stop|Exit)')
FILE_OR_KEY = re.compile(r'file|regist|reg|create|open|query|irp', re.I)


class Refused(Exception):
    pass


def norm(record):
    return ' '.join(record.split())


def _text_chunks(path, max_bytes, deadline):
    with open(path, 'rb') as f:
        head = f.read(4)
        f.seek(0)
        enc = 'utf-16' if head[:2] in (b'\xff\xfe', b'\xfe\xff') else 'utf-8'
        dec = codecs.getincrementaldecoder(enc)(errors='replace')
        seen = 0
        while True:
            if deadline is not None and time.monotonic() > deadline:
                raise Refused('time cap reached')
            chunk = f.read(CHUNK)
            if not chunk:
                tail = dec.decode(b'', final=True)
                if tail:
                    yield tail
                return
            seen += len(chunk)
            if seen > max_bytes:
                raise Refused('dump over size cap bytes=%d' % seen)
            yield dec.decode(chunk)


def iter_records(chunks):
    """Yield one normalized record at a time. XML mode when an <Event> element is seen, else one per line."""
    buf = ''
    mode = None
    for text in chunks:
        buf += text
        if mode is None:
            if EVENT_OPEN.search(buf):
                mode = 'xml'
            elif len(buf) < 4096 and '\n' not in buf:
                continue
            else:
                mode = 'line'
        if mode == 'xml':
            pos = 0
            while True:
                m = EVENT_OPEN.search(buf, pos)
                if not m:
                    buf = buf[max(pos, len(buf) - 8):]
                    break
                end = buf.find('</Event>', m.start())
                if end < 0:
                    if len(buf) - m.start() > MAX_RECORD:
                        pos = m.start() + 6
                        continue
                    buf = buf[m.start():]
                    break
                yield norm(buf[m.start():end + 8])
                pos = end + 8
        else:
            lines = buf.split('\n')
            buf = lines.pop()
            if len(buf) > MAX_RECORD:
                buf = ''
            for line in lines:
                if line.strip():
                    yield norm(line)
    if mode == 'line' and buf.strip():
        yield norm(buf)
    if mode is None and buf.strip():
        yield norm(buf)


def records(text):
    """Test helper: records of an in-memory text through the same streaming splitter."""
    return list(iter_records([text]))


def pid_of(record):
    m = PID_XML.search(record) or PID_VALUE.search(record)
    return int(m.group(1), 0) if m else None


def has_pid(record, pid):
    return any(int(v, 0) == pid for v in PID_XML.findall(record) + PID_VALUE.findall(record))


def cut(line, n=LINE_CUT):
    return line[:n]


def irp_of(record):
    m = IRP.search(record)
    return (m.group(1) or m.group(2)).lower() if m else None


def name_of(record):
    m = EVENT_NAME.search(record)
    if not m:
        return 'unknown'
    return (m.group(1) or m.group(2) or m.group(3) or 'unknown').strip()


def analyze_stream(recs, token, truncated=False, deadline=None):
    token = token.lower()
    children = set()
    counts = {}
    total = py_starts = token_records = pid_events = 0
    images = collections.deque(maxlen=TAIL_IMAGES)
    stops = collections.deque(maxlen=TAIL_STOPS)
    irp_map = collections.OrderedDict()
    irp_dropped = 0
    denied = []
    denied_total = denied_no_create = 0
    start_samples = []
    for rec in recs:
        total += 1
        if deadline is not None and (total == 1 or (total & 1023) == 0) and time.monotonic() > deadline:
            raise Refused('time cap reached')
        low = rec.lower()
        pid = pid_of(rec)
        is_py_start = 'python.exe' in low and START.search(rec) and not NOT_START.search(rec)
        if is_py_start:
            py_starts += 1
            if len(start_samples) < MAX_START_SAMPLES:
                start_samples.append(rec)
            if token in low:
                token_records += 1
                if pid is not None:
                    children.add(pid)
        in_child = pid is not None and pid in children
        irp = irp_of(rec)
        if in_child:
            pid_events += 1
            key = name_of(rec)
            if key in counts or len(counts) < MAX_EVENT_NAMES:
                counts[key] = counts.get(key, 0) + 1
            if IMAGE_LOAD.search(rec) and LOAD.search(rec):
                images.append(rec)
            if STOP_WORD.search(rec) and EXIT_STATUS.search(rec) and PROCESS_WORD.search(rec):
                stops.append(rec)
        is_denied = bool(DENIED.search(rec))
        if in_child and not is_denied and irp and OPEN_LIKE.search(rec):
            irp_map[irp] = rec
            irp_map.move_to_end(irp)
            while len(irp_map) > IRP_MAP_CAP:
                irp_map.popitem(last=False)
                irp_dropped += 1
        if is_denied and ((in_child and FILE_OR_KEY.search(rec)) or (irp and irp in irp_map)):
            denied_total += 1
            opened = irp_map.get(irp) if irp else None
            if opened is None:
                denied_no_create += 1
            if len(denied) < MAX_DENIED_SHOWN:
                denied.append((rec, opened))
    return {'children': sorted(children), 'total': total, 'py_starts': py_starts, 'token_records': token_records,
            'pid_events': pid_events, 'images': list(images), 'stops': list(stops), 'counts': counts,
            'denied': denied, 'denied_total': denied_total, 'denied_no_create': denied_no_create,
            'irp_dropped': irp_dropped, 'start_samples': start_samples, 'truncated': truncated}


def render(r):
    out = []
    ch = ','.join(str(p) for p in r['children']) or 'none'
    out.append('trace_counts total_events=%d child_pids=%s pid_events=%d denied_events=%d denied_without_create=%d python_start_records=%d token_start_records=%d irp_map_dropped=%d trace_truncated=%s' % (
        r['total'], ch, r['pid_events'], r['denied_total'], r['denied_no_create'], r['py_starts'], r['token_records'], r['irp_dropped'], 'yes' if r['truncated'] else 'no'))
    if not r['children']:
        for s in r['start_samples']:
            out.append('trace_python_start ' + cut(s, START_CUT))
        out.append('trace_reading 3_no_child_pid_or_session_empty=inconclusive no_rerun')
    else:
        for name, n in sorted(r['counts'].items(), key=lambda kv: (-kv[1], kv[0]))[:30]:
            out.append('trace_child_event_count name=%s count=%d' % (name, n))
        for rec, opened in r['denied']:
            if len(out) >= MAX_OUT_LINES - TAIL_IMAGES - TAIL_STOPS - 8:
                out.append('trace_denied_output_capped=1')
                break
            if opened:
                out.append('trace_open ' + cut(opened))
            out.append('trace_denied ' + cut(rec))
        if r['denied_total'] > len(r['denied']):
            out.append('trace_denied_output_capped=1')
        for rec in r['images']:
            out.append('trace_image_load ' + cut(rec))
        for rec in r['stops']:
            out.append('trace_process_stop ' + cut(rec))
        if r['denied_total']:
            out.append('trace_reading 1_denied_open_with_object=named_object_only;fix_is_a_separate_design_needing_review_and_owner_consent')
        else:
            out.append('trace_reading 2_no_denied_file_open=denial_not_a_file_open_in_captured_events;last_images_show_loader_progress;instrument_exhausted_report_to_owner')
    out.append('trace_limits diagnostic_only=1 negative_inconclusive=1 security_acceptance=NOT_ESTABLISHED no_cause_claimed_beyond_a_named_object=1 no_rerun=1 no_environment_values_printed=1')
    if len(out) > MAX_OUT_LINES:
        out = out[:MAX_OUT_LINES - 1] + [out[-1]]
    return out


def analyze(text, token, truncated=False, deadline=None):
    """In-memory convenience used by tests; the CLI streams from the file."""
    return render(analyze_stream(iter_records([text]), token, truncated, deadline))


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument('--dump', required=True)
    p.add_argument('--token', required=True)
    p.add_argument('--etl-bytes', type=int, default=0)
    p.add_argument('--max-bytes', type=int, default=MAX_BYTES)
    p.add_argument('--max-seconds', type=int, default=MAX_SECONDS)
    a = p.parse_args(argv)
    if not re.fullmatch(r'[A-Za-z0-9._-]{8,80}', a.token):
        print('trace_analysis_refused reason=bad_token')
        return 2
    try:
        deadline = time.monotonic() + a.max_seconds
        r = analyze_stream(iter_records(_text_chunks(a.dump, a.max_bytes, deadline)), a.token,
                           truncated=a.etl_bytes >= TRUNC_BYTES, deadline=deadline)
        lines = render(r)
    except (Refused, OSError) as exc:
        print('trace_analysis_refused reason=' + str(exc)[:80] + ' reading=4_capture_ok_analysis_refused inconclusive=1 no_rerun=1')
        return 2
    for line in lines:
        print(cut(line))
    return 0


if __name__ == '__main__':
    sys.exit(main())
