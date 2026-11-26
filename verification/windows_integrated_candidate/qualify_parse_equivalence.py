"""Isolated Windows qualification harness for candidate_parent.parse_candidate.

Never a product entrypoint and never acceptance. It runs the SAME six payloads as
tests/test_fingerprint_bounded_parse.py through the real candidate and compares each
result with the direct parser, then records three bounded-failure cases and package
versions. Output is fixed-vocabulary key=value lines only (no raw payload, path or
error text). A refusal or mismatch is a result for the integrator, not a reason to
loosen a limit. security_acceptance stays NOT_ESTABLISHED.
"""
import base64
from hashlib import sha256
from datetime import datetime
from io import BytesIO
import json
import re
import sys
import time

ALIASES = {'SYN': 1}
OUTPUT_LIMIT = 4_000_000
INPUT_LIMIT = 16_000_000


def _csv(rows):
    return ('\n'.join(','.join(v) for v in rows) + '\n').encode()


def _xlsx(rows):
    from openpyxl import Workbook
    book = Workbook()
    sheet = book.active
    sheet.title = 'Synthetic'
    for row in rows:
        sheet.append(row)
    out = BytesIO()
    book.save(out)
    return out.getvalue()


def equivalence_payloads():
    """Six contract cases (csv duplicates/unknown, csv+xlsx overnight, xlsx datetime/0.5/None)
    plus one near-output-limit valid csv (350 rows, about 3.8 MB of output) to measure headroom."""
    dup = b'ID,Date,Time\nOTHER,2026-11-01,09:00\nSYN,2026-11-01,08:00 08:00 16:00\n'
    overnight = [['ID', 'Date', 'Time'], ['OTHER', '2026-11-01', '09:00'],
                 ['SYN', '2026-11-01', '23:00 23:00 bad'], ['SYN', '2026-11-02', '07:00']]
    cases = [('csv_duplicates_unknown', 'synthetic.csv', dup),
             ('csv_overnight', 'synthetic.csv', _csv(overnight)),
             ('xlsx_overnight', 'synthetic.xlsx', _xlsx(overnight)),
             ('csv_near_output_limit', 'synthetic.csv', _dense_csv(350))]
    from openpyxl import Workbook
    for label, value in (('xlsx_datetime', datetime(2026, 11, 1, 8)), ('xlsx_numeric', 0.5), ('xlsx_blank', None)):
        book = Workbook()
        sheet = book.active
        sheet.title = 'Synthetic'
        sheet.append(['ID', 'Date', 'Time'])
        sheet.append(['SYN', '2026-11-01', value])
        sheet.append(['SYN', '2026-11-02', '07:00'])
        book.create_sheet('Blank')
        out = BytesIO()
        book.save(out)
        cases.append((label, 'synthetic.xlsx', out.getvalue()))
    return cases


def request_bytes(raw, filename, aliases=None):
    return json.dumps({'payload': base64.b64encode(raw).decode('ascii'), 'filename': filename,
                       'aliases': ALIASES if aliases is None else aliases}, separators=(',', ':')).encode()


def failure_requests():
    """Bounded-failure requests that use only the existing worker.

    nonzero_exit: exact-schema violation; the worker raises and exits nonzero.
    oversize_output: 500 rows x 20 punches gives about 5.4 MB of JSON, over the 4,000,000 B
    output limit, so the worker raises and exits nonzero (calibrated against the direct parser).
    wall_kill is NOT exercised: a deterministic 3 s sleep needs a harness-only worker in a
    separately staged runtime, which this harness does not create. It is reported as
    not_exercised, never as passed.
    """
    bad = json.dumps({'payload': '', 'filename': 'synthetic.csv'}).encode()
    return [('nonzero_exit', bad), ('oversize_output', request_bytes(_dense_csv(500), 'synthetic.csv'))]


def _dense_csv(rows):
    row = b'SYN,2026-11-01,' + b' '.join([b'08:00'] * 20) + b'\n'
    return b'ID,Date,Time\n' + row * rows


def classify(error):
    """Fixed category from the exception. Never returns raw text."""
    text = str(error)
    if 'wall budget' in text:
        return 'wall_budget'
    if 'Native diagnostic budget' in text:
        return 'diagnostic_budget'
    if 'Candidate refused code=' in text:
        return 'refused_nonzero'
    if 'exceeds safety budget' in text:
        return 'output_budget'
    return 'other_refusal' if isinstance(error, ValueError) else 'unexpected_error'


def canonical_direct(evidence, raw):
    """FingerprintEvidence dataclass -> (period_start, period_end, events, issues) as lists.

    The dataclass also carries source_sha256 and original_bytes, which the candidate tuple does
    not. They are checked here against the input (reference-side sanity) and then dropped.
    """
    if evidence.original_bytes != raw or evidence.source_sha256 != sha256(raw).hexdigest():
        raise AssertionError('direct reference does not echo the input')
    return (evidence.period_start, evidence.period_end, [dict(v) for v in evidence.occurrences],
            [dict(v) for v in evidence.issues])


def canonical_candidate(result):
    """parse_candidate result (first, last, event list, issue list) -> same canonical form, else None.

    None means the candidate did not return the _decode_worker 4-tuple contract and counts as DIFFERENT.
    """
    if type(result) is not tuple or len(result) != 4 or type(result[2]) is not list or type(result[3]) is not list:
        return None
    return (result[0], result[1], [dict(v) for v in result[2]], [dict(v) for v in result[3]])


# Stage names the native launcher can print in CANDIDATE_REFUSAL (grounded in launcher.c by a test).
REFUSAL_STAGES = frozenset(('arguments', 'attributes', 'continue_initial_debug_event', 'create_suspended', 'derive_profile',
                            'duplex_wait', 'environment', 'hold_image', 'hold_initial_debug_event', 'job', 'pipes', 'read_input',
                            'resume_before_first_creation_event', 'start_feeder', 'start_reader', 'startup_fixed_real_open',
                            'startup_readonly_diagnostics', 'verify_after_attach_held_event', 'verify_before_resume'))
REFUSAL_LINE = re.compile(r'^CANDIDATE_REFUSAL stage=([a-z_]{1,48}) error=([0-9]{1,10}) child=([0-9]{1,10})$')
CREATED_LINE = re.compile(r'^CANDIDATE_CREATED pid=[0-9]{1,10} suspended=1$')
CODE_FIELD = re.compile(r'^Candidate refused code=(-?[0-9]{1,10}) native=')


def refusal_line(label, error):
    """One fixed-vocabulary qual_refusal line from a ValueError raised by candidate_parent.

    Only the launcher exit code, an allowlisted CANDIDATE_REFUSAL stage, its numeric error/child
    and whether the child was created (0/1) are printed. Raw stderr, paths and pids are never shown.
    An unknown stage prints stage=unparsed; a missing launcher line prints stage=none.
    """
    text = str(error)
    code = CODE_FIELD.match(text)
    native = text.split(' native=', 1)[1] if ' native=' in text else ''
    created = 0
    stage, os_error, child = 'none', 'none', 'none'
    for line in native.splitlines():
        if CREATED_LINE.match(line):
            created = 1
        found = REFUSAL_LINE.match(line)
        if found:
            if found.group(1) in REFUSAL_STAGES:
                stage, os_error, child = found.groups()
            else:
                stage, os_error, child = 'unparsed', 'none', 'none'
    return 'qual_refusal label=%s launcher_code=%s stage=%s error=%s child=%s created=%d' % (
        label, code.group(1) if code else 'none', stage, os_error, child, created)


EXCEPTION_LINE = re.compile(r'^loader_debug_exception code=([0-9a-f]{8}) first_chance=([01])$')
STRING_READABLE = re.compile(r'^loader_debug_string readable=1 fixed_image_bits=[0-9]{1,10} bounded_bytes=[0-9]{1,10} unicode=[01] clamped=[01] raw_discarded=1 keyword_bits=[0-9]{1,10} ldr_prefix=[01]((?: ntstatus_[0-3]=[0-9a-f]{8}){0,4})$')
STRING_UNREADABLE = re.compile(r'^loader_debug_string readable=0 declared_bytes=[0-9]{1,10} received_bytes=[0-9]{1,10} unicode=[01] no_localization_claim=1$')
EXIT_LINE = re.compile(r'^loader_debug_exit code=([0-9a-f]{8})$')
EVENT_LINE = re.compile(r'^loader_debug_event kind=([0-9]{1,2}) ordinal=([0-9]{1,6})$')
STDERR_CAP_NEAR = 15000


def loader_line(label, error):
    """One fixed-vocabulary qual_loader line from loader_debug lines the launcher already prints.

    The launcher's debug events carry NO module identity: they are counts and ordinals only. So this
    line never names or attributes a DLL. It reports how many debugger events of each kind were
    captured, the first exception code, the first ntstatus_N value inside any readable debug string
    (a value the child printed, not a proven cause), and the debugger exit code. A kind count of 0
    means "not captured" (stderr is bounded and may be truncated: stderr_near_cap=1), not "did not
    happen". Only strictly matched whole lines count; everything else is ignored. No raw text.
    """
    text = str(error)
    native = text.split(' native=', 1)[1] if ' native=' in text else ''
    native_state = ('budget_exceeded' if 'Native diagnostic budget exceeded' in text
                    else 'present' if ' native=' in text else 'absent')
    kinds = {'1': 0, '6': 0, '7': 0, '8': 0}
    events = last_ordinal = exceptions = readable = unreadable = 0
    first_exception = exit_code = first_ntstatus = 'none'
    for line in native.splitlines():
        found = EVENT_LINE.match(line)
        if found:
            events += 1
            last_ordinal = int(found.group(2))
            if found.group(1) in kinds:
                kinds[found.group(1)] += 1
            continue
        found = EXCEPTION_LINE.match(line)
        if found:
            exceptions += 1
            if first_exception == 'none':
                first_exception = found.group(1)
            continue
        found = STRING_READABLE.match(line)
        if found:
            readable += 1
            values = re.findall(r'=([0-9a-f]{8})', found.group(1))
            if values and first_ntstatus == 'none':
                first_ntstatus = values[0]
            continue
        if STRING_UNREADABLE.match(line):
            unreadable += 1
            continue
        found = EXIT_LINE.match(line)
        if found:
            exit_code = found.group(1)
    return ('qual_loader label=%s native=%s events=%d last_ordinal=%d load_dll=%d unload_dll=%d debug_string_events=%d '
            'exception_events=%d first_exception=%s strings_readable=%d strings_unreadable=%d '
            'first_string_ntstatus=%s debug_exit=%s stderr_near_cap=%d' % (
                label, native_state, events, last_ordinal, kinds['6'], kinds['7'], kinds['8'], exceptions, first_exception,
                readable, unreadable, first_ntstatus, exit_code, int(len(native) >= STDERR_CAP_NEAR)))


HEADROOM_LABEL = 'csv_near_output_limit'
EXPECTED_BUDGET = {'nonzero_exit': {'refused_nonzero'}, 'oversize_output': {'refused_nonzero', 'output_budget'}}


def run(parse, direct, clock=time.perf_counter):
    """parse(request_bytes) -> result or raises ValueError; direct(raw, filename) -> result.

    Returns (flags, lines). flags has contract6_equal, headroom_equal and budget_ok. Every
    flag must be true for a green run. A budget case that RETURNS or refuses for another
    reason is a failure, not a recorded curiosity.
    """
    lines = []
    refusals = []
    contract6 = True
    headroom = True
    for label, filename, raw in equivalence_payloads():
        expected = canonical_direct(direct(raw, filename), raw)
        start = clock()
        try:
            actual = parse(request_bytes(raw, filename))
            state = 'equal' if canonical_candidate(actual) == expected else 'DIFFERENT'
        except ValueError as exc:
            category = classify(exc)
            state = category if category.startswith('refused_') else 'refused_' + category
            refusals.append(refusal_line(label, exc))
            if len(refusals) == 1:
                refusals.append(loader_line(label, exc))
        wall_ms = int((clock() - start) * 1000)
        if label == HEADROOM_LABEL:
            headroom = headroom and state == 'equal'
        else:
            contract6 = contract6 and state == 'equal'
        lines.append('qual_equiv label=%s result=%s wall_ms=%d' % (label, state, wall_ms))
    lines.extend(refusals)
    budget_ok = True
    for label, request in failure_requests():
        start = clock()
        try:
            parse(request)
            state = 'returned'
        except ValueError as exc:
            state = classify(exc)
            if label == 'nonzero_exit':
                lines.append(refusal_line(label, exc))
        wall_ms = int((clock() - start) * 1000)
        budget_ok = budget_ok and state in EXPECTED_BUDGET[label]
        lines.append('qual_budget label=%s category=%s expected=%d checked=%d wall_ms=%d' % (label, state, 1 if state in EXPECTED_BUDGET[label] else 0, 1 if contract6 else 0, wall_ms))
    lines.append('qual_budget label=wall_kill category=not_exercised expected=0 checked=0 wall_ms=0')
    # A budget refusal only means something if valid input was accepted: when the launcher refuses
    # everything at startup, the bad-input checks prove nothing, so budget_ok is withheld.
    budget_checked = contract6
    return {'contract6_equal': contract6, 'headroom_equal': headroom, 'budget_checked': budget_checked,
            'budget_ok': budget_checked and budget_ok}, lines


def summary_line(flags, packages_ok):
    values = dict(flags, packages_ok=packages_ok)
    parts = ' '.join('%s=%d' % (key, 1 if values[key] else 0)
                     for key in ('contract6_equal', 'headroom_equal', 'budget_checked', 'budget_ok', 'packages_ok'))
    return 'qual_summary %s all_green=%d wall_kill=not_exercised cpu_ms=not_measured package_basis=trusted_host_vendor_vs_lock security_acceptance=NOT_ESTABLISHED' % (
        parts, 1 if all(values.values()) else 0)


def package_versions(vendor_lock):
    """Package metadata visible to the trusted host interpreter vs ACQUISITION-LOCK.json (trusted_host_vendor_vs_lock).

    Under the workflow this is vendor-path metadata (PYTHONPATH of the acquired wheels), not an
    independent host pip version.

    This is NOT introspection of the staged runtime. Staged content is tied to the lock only
    through stage_runtime and the pinned manifest hash verified by candidate_parent.
    Returns (all_match, lines). An absent host package or any mismatch gives all_match False.
    """
    from importlib import metadata
    lock = json.loads(vendor_lock)
    lines = []
    all_match = True
    for name in ('openpyxl', 'xlrd', 'et_xmlfile'):
        try:
            host = metadata.version(name)
        except metadata.PackageNotFoundError:
            host = 'absent'
        locked = lock[name]['version']
        match = host == locked
        all_match = all_match and match
        lines.append('qual_package basis=trusted_host_vendor_vs_lock name=%s host=%s lock=%s match=%d' % (name, host, locked, 1 if match else 0))
    lines.append('qual_python host=%d.%d.%d lock=%s judged=0' % (sys.version_info[0], sys.version_info[1], sys.version_info[2], lock['python']['version']))
    return all_match, lines


def evaluate(parse, direct, vendor_lock, clock=time.perf_counter):
    """Whole run. Returns (exit_code, lines): 0 only when every gate is true."""
    packages_ok, package_lines = package_versions(vendor_lock)
    flags, lines = run(parse, direct, clock)
    out = package_lines + lines + [summary_line(flags, packages_ok)]
    return (0 if packages_ok and all(flags.values()) else 1), out


def main(argv):
    from pathlib import Path
    from app.fingerprint_evidence import parse_fingerprint_evidence
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import candidate_parent
    if len(argv) != 7:
        print('qual_usage argc=%d' % len(argv))
        return 2
    launcher, profile, stage, manifest_pin, launcher_pin, lock_path = argv[1:7]

    def parse(request):
        return candidate_parent.parse_candidate(launcher, profile, stage, manifest_pin, launcher_pin, request, ALIASES)

    def direct(raw, filename):
        return parse_fingerprint_evidence(raw, filename=filename, verified_aliases=ALIASES)

    code, lines = evaluate(parse, direct, Path(lock_path).read_text())
    if any(line.startswith('qual_refusal') for line in lines):
        try:
            import qualify_plain_control
            lines.append(qualify_plain_control.guarded_plain_control(candidate_parent, stage, manifest_pin, equivalence_payloads, request_bytes, ALIASES))
        except Exception:
            lines.append('qual_plain label=none exit=none out_bytes=0 decode=na error_class=harness')
    for line in lines:
        print(line)
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv))
