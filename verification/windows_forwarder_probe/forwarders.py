"""Runner-side, read-only target list for the forwarder probe. Parses dumpbin text only.
Never writes under System32, never loads a DLL, never launches the sandbox. Output: targets.h, targets.txt.
Refuses (exit 2) on an empty, malformed or oversize result so nothing is launched."""
import argparse
import os
import re
import subprocess
import sys

MAX_TEXT = 8_000_000
MAX_LINES = 400_000
CAP = 20
MAX_DEPTH = 3

# Export tables are read only for these six system hosts. Imports of python314.dll are mapped onto them.
HOSTS = ('advapi32', 'kernel32', 'version', 'ws2_32', 'bcrypt', 'ucrtbase')
CRT_SETS = ['api-ms-win-crt-%s-l1-1-0' % n for n in (
    'runtime', 'stdio', 'environment', 'string', 'heap', 'locale', 'convert', 'math', 'time', 'process', 'conio', 'filesystem')]
# Never probed as a target: resident or already proven by earlier runs.
DROP = frozenset(['ntdll', 'kernelbase', 'kernel32', 'version', 'ws2_32', 'advapi32', 'vcruntime140', 'bcrypt',
                  'api-ms-win-core-path-l1-1-0'] + CRT_SETS)

# Named-import counts of python314.dll per DLL, computed from the pinned embed zip. Any difference refuses.
# WS2_32 is imported by ordinal only (3 imports), so its named count is 0 and ordinal imports are not covered.
IMPORT_PINS = {'kernel32': 186, 'advapi32': 22, 'version': 3, 'bcrypt': 1, 'api-ms-win-core-path-l1-1-0': 2,
               'vcruntime140': 12, 'ws2_32': 0,
               'api-ms-win-crt-stdio-l1-1-0': 40, 'api-ms-win-crt-environment-l1-1-0': 6, 'api-ms-win-crt-string-l1-1-0': 21,
               'api-ms-win-crt-runtime-l1-1-0': 28, 'api-ms-win-crt-heap-l1-1-0': 7, 'api-ms-win-crt-locale-l1-1-0': 2,
               'api-ms-win-crt-convert-l1-1-0': 5, 'api-ms-win-crt-math-l1-1-0': 39, 'api-ms-win-crt-time-l1-1-0': 10,
               'api-ms-win-crt-process-l1-1-0': 5, 'api-ms-win-crt-conio-l1-1-0': 8, 'api-ms-win-crt-filesystem-l1-1-0': 2}
CHAIN_NOTE = ('# chains are followed only through the six dumped hosts (real depth is 1 for forwards into other files); '
              'ws2_32 ordinal imports are not covered; model only from dumpbin text')

NAME_OK = re.compile(r'^[a-z0-9][a-z0-9_-]{0,58}\.dll$')
STEM_OK = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,58}$')
DLL_HEADER = re.compile(r'^ {4}(\S+)\.dll\s*$', re.I)
IMPORT_FUNC = re.compile(r'^ {10,}([0-9A-Fa-f]+) {1,}(\S+)\s*$')
IMPORT_SKIP = re.compile(r'Import Address Table|Import Name Table|time date stamp|Index of first forwarder|^\s+Ordinal\s+\d+', re.I)
EXPORT_LINE = re.compile(r'^ +(\d+) +([0-9A-Fa-f]+) +(?:[0-9A-Fa-f]{8} +)?(\S+)(?: += +\S+)?(?: +\(forwarded to (\S+)\))? *$')


class Refused(Exception):
    pass


def _check_text(text):
    if type(text) is not str or len(text) > MAX_TEXT or text.count('\n') > MAX_LINES or '\x00' in text:
        raise Refused('dumpbin text oversize or invalid')


def parse_imports(text):
    """Return [(dll_stem_lower, function)] for named imports, in file order."""
    return parse_imports_full(text)[0]


def parse_imports_full(text):
    """Return (named imports, every DLL header seen) so a DLL with zero named imports is still counted."""
    _check_text(text)
    out = []
    current = None
    seen = []
    for line in text.splitlines():
        m = DLL_HEADER.match(line)
        if m:
            stem = m.group(1)
            if not STEM_OK.match(stem):
                raise Refused('malformed import dll name')
            current = stem.lower()
            seen.append(current)
            continue
        if current is None or IMPORT_SKIP.search(line):
            continue
        m = IMPORT_FUNC.match(line)
        if m:
            out.append((current, m.group(2)))
    return out, seen


def check_import_counts(imports, headers, pins=IMPORT_PINS):
    counts = {}
    for stem, _func in imports:
        counts[stem] = counts.get(stem, 0) + 1
    for stem in headers:
        counts.setdefault(stem, 0)
    if counts != dict(pins):
        raise Refused('import counts differ from pinned counts')


EXPORT_NAMES = re.compile(r'^ +([0-9]+) +number of names *$', re.M)


def parse_exports(text, require_header=True):
    """Return {function: (host_stem_lower, target_function) or None}. None means not forwarded."""
    _check_text(text)
    table = {}
    parsed = 0
    for line in text.splitlines():
        m = EXPORT_LINE.match(line)
        if not m:
            continue
        parsed += 1
        name, target = m.group(3), m.group(4)
        if target is None:
            table[name] = None
            continue
        host, dot, func = target.partition('.')
        if not dot or not func or not STEM_OK.match(host):
            raise Refused('malformed forwarder target')
        table[name] = (host.lower(), func)
    if require_header:
        header = EXPORT_NAMES.findall(text)
        if len(header) != 1 or parsed < int(header[0]):
            raise Refused('parsed export count below the header number of names')
    return table


def import_host(stem):
    if stem in HOSTS:
        return stem
    if stem in CRT_SETS:
        return 'ucrtbase'
    return None


def build_targets(imports, exports_by_host):
    """Return (sorted_names, forwarded_by, overflow). forwarded_by maps name -> sorted 'host!function' strings."""
    forwarded_by = {}
    for stem, func in imports:
        host = import_host(stem)
        if host is None or host not in exports_by_host:
            continue
        origin = host + '!' + func
        current_host, current_func = host, func
        for _depth in range(MAX_DEPTH):
            entry = exports_by_host.get(current_host, {}).get(current_func)
            if entry is None:
                break
            target_host, target_func = entry
            if target_host not in DROP:
                forwarded_by.setdefault(target_host + '.dll', set()).add(origin)
            current_host, current_func = target_host, target_func
    names = sorted(forwarded_by)
    for name in names:
        if not NAME_OK.match(name):
            raise Refused('target name outside the allowed shape')
    return names[:CAP], {k: sorted(v) for k, v in forwarded_by.items()}, names[CAP:]


def render_header(names):
    if not 1 <= len(names) <= CAP:
        raise Refused('target count out of range')
    rows = ',\n'.join('    L"%s"' % n for n in names)
    return ('/* Generated by forwarders.py. Names are validated to ^[a-z0-9][a-z0-9_-]*\\.dll$. */\n'
            '#define TARGET_COUNT %du\n'
            'static const wchar_t *const targets[TARGET_COUNT] = {\n%s\n};\n') % (len(names), rows)


def render_listing(names, forwarded_by, overflow):
    lines = [CHAIN_NOTE] + ['target index=%d name=%s forwarded_by=%s' % (i + 1, n, ';'.join(forwarded_by[n])) for i, n in enumerate(names)]
    lines += ['unprobed_overflow name=%s forwarded_by=%s' % (n, ';'.join(forwarded_by[n])) for n in overflow]
    return '\n'.join(lines) + '\n'


def run_dumpbin(dumpbin, args):
    result = subprocess.run([dumpbin, '/nologo'] + args, capture_output=True, text=True, timeout=90, check=False)
    if result.returncode != 0:
        raise Refused('dumpbin failed')
    return result.stdout


def main(argv=None, runner=run_dumpbin, pins=IMPORT_PINS):
    p = argparse.ArgumentParser()
    p.add_argument('--dumpbin', required=True)
    p.add_argument('--python-dll', required=True)
    p.add_argument('--system32', required=True)
    p.add_argument('--out-dir', required=True)
    a = p.parse_args(argv)
    try:
        imports, headers = parse_imports_full(runner(a.dumpbin, ['/imports', a.python_dll]))
        if not imports:
            raise Refused('no imports parsed')
        check_import_counts(imports, headers, pins)
        exports = {}
        for host in HOSTS:
            exports[host] = parse_exports(runner(a.dumpbin, ['/exports', os.path.join(a.system32, host + '.dll')]))
            if not exports[host]:
                raise Refused('no exports parsed for ' + host)
        names, forwarded_by, overflow = build_targets(imports, exports)
        header = render_header(names)
        listing = render_listing(names, forwarded_by, overflow)
    except (Refused, OSError, subprocess.SubprocessError) as exc:
        print('forwarder_targets_refused reason=%s launch=NOT_ALLOWED' % str(exc)[:60])
        return 2
    with open(os.path.join(a.out_dir, 'targets.h'), 'w', newline='\n') as f:
        f.write(header)
    with open(os.path.join(a.out_dir, 'targets.txt'), 'w', newline='\n') as f:
        f.write(listing)
    print('forwarder_targets count=%d overflow_unprobed=%d imports_parsed=%d model_only_names_from_dumpbin=1' % (len(names), len(overflow), len(imports)))
    sys.stdout.write(listing)
    return 0


if __name__ == '__main__':
    sys.exit(main())
