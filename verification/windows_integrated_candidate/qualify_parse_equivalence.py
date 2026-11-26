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


HEADROOM_LABEL = 'csv_near_output_limit'
EXPECTED_BUDGET = {'nonzero_exit': {'refused_nonzero'}, 'oversize_output': {'refused_nonzero', 'output_budget'}}


def run(parse, direct, clock=time.perf_counter):
    """parse(request_bytes) -> result or raises ValueError; direct(raw, filename) -> result.

    Returns (flags, lines). flags has contract6_equal, headroom_equal and budget_ok. Every
    flag must be true for a green run. A budget case that RETURNS or refuses for another
    reason is a failure, not a recorded curiosity.
    """
    lines = []
    contract6 = True
    headroom = True
    for label, filename, raw in equivalence_payloads():
        expected = canonical_direct(direct(raw, filename), raw)
        start = clock()
        try:
            actual = parse(request_bytes(raw, filename))
            state = 'equal' if canonical_candidate(actual) == expected else 'DIFFERENT'
        except ValueError as exc:
            state = 'refused_' + classify(exc)
        wall_ms = int((clock() - start) * 1000)
        if label == HEADROOM_LABEL:
            headroom = headroom and state == 'equal'
        else:
            contract6 = contract6 and state == 'equal'
        lines.append('qual_equiv label=%s result=%s wall_ms=%d' % (label, state, wall_ms))
    budget_ok = True
    for label, request in failure_requests():
        start = clock()
        try:
            parse(request)
            state = 'returned'
        except ValueError as exc:
            state = classify(exc)
        wall_ms = int((clock() - start) * 1000)
        budget_ok = budget_ok and state in EXPECTED_BUDGET[label]
        lines.append('qual_budget label=%s category=%s expected=%d wall_ms=%d' % (label, state, 1 if state in EXPECTED_BUDGET[label] else 0, wall_ms))
    lines.append('qual_budget label=wall_kill category=not_exercised expected=0 wall_ms=0')
    return {'contract6_equal': contract6, 'headroom_equal': headroom, 'budget_ok': budget_ok}, lines


def summary_line(flags, packages_ok):
    values = dict(flags, packages_ok=packages_ok)
    parts = ' '.join('%s=%d' % (key, 1 if values[key] else 0)
                     for key in ('contract6_equal', 'headroom_equal', 'budget_ok', 'packages_ok'))
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
    for line in lines:
        print(line)
    return code


if __name__ == '__main__':
    sys.exit(main(sys.argv))
