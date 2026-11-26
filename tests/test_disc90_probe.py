"""Static and logic checks of the disc90 diagnostic packet. Linux only: nothing here runs Windows code.
The C probe is NOT compiled or run here; these tests cover the decoder, pins, import order, staging refusals,
workflow equivalence and banned strings. Not acceptance."""
import hashlib
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / 'verification/windows_disc90_probe'
sys.path.insert(0, str(D))
import decode_disc90 as dec  # noqa: E402
import pe_imports  # noqa: E402
import stage_disc90 as stg  # noqa: E402


def code(bits=(), cls=None, idx=0):
    value = 0x20000000
    for b in bits:
        value |= 1 << b
    for (name, _b, shift), c in zip(dec.CLASS_FIELDS, cls or [0] * 5):
        value |= c << shift
    return value | (idx << 24)


ALL_OK = list(range(14))


def test_all_ok_decodes_and_baseline_not_reproduced():
    d = dec.decode(code(ALL_OK))
    assert d['bits'] == [1] * 14 and d['s7_first_fail'] == 0
    assert 'R5 baseline_failure_not_reproduced' in dec.readings(d)


def test_each_bit_round_trips():
    for b in range(14):
        # A lone set bit is only consistent if its step has no failing class; others failing need a class.
        bits = [x for x in ALL_OK if x != b]
        cls = [0] * 5
        for i, (_n, sb, _s) in enumerate(dec.CLASS_FIELDS):
            if sb == b:
                cls[i] = 1
        if b == 12 and 11 in bits:
            continue  # S8a ok with S8b failed is refused (covered below)
        if b == 5:
            continue  # S6 detail bits without S6a are refused (covered below)
        d = dec.decode(code(bits, cls))
        assert d['bits'][b] == 0 and sum(d['bits']) == 13


def test_error_classes_and_index():
    for c in (1, 2, 3):
        d = dec.decode(code([x for x in ALL_OK if x != 4], [0, 0, c, 0, 0]))
        assert d['classes']['S4'] == c
    for i in range(1, 20):
        assert dec.decode(code(ALL_OK, idx=i))['s7_first_fail'] == i


@pytest.mark.parametrize('value', [
    0, 5, 3221225506, 0x40000000, 0x80000000, 0x3FFFFFFF + 1, 0x00000001, 0x20000000 | (31 << 24) | 0x3FFF,
    0x20000000 | (20 << 24) | 0x3FFF,  # index 20
    code([x for x in ALL_OK if x != 4]),  # failed step without class
    code(ALL_OK, [0, 0, 1, 0, 0]),  # class without failed step
    code([x for x in ALL_OK if x not in (11, 12)] + [11]),  # S8a ok but S8b failed
    code([x for x in ALL_OK if x != 5]),  # S6 detail bits without S6a
])
def test_non_parsing_refused(value):
    with pytest.raises(ValueError):
        dec.decode(value)


def test_exact_marker_only_is_refusal_not_result():
    assert dec.decode(0x20000000) == {'refused': True}


def test_cli_refuses_and_prints_raw(tmp_path):
    out = subprocess.run([sys.executable, '-B', str(D / 'decode_disc90.py'), '--exit', '3221225506'], capture_output=True, text=True)
    assert out.returncode == 2 and 'disc90_decode_refused raw_exit=3221225506' in out.stdout
    out = subprocess.run([sys.executable, '-B', str(D / 'decode_disc90.py'), '--exit', 'x1'], capture_output=True, text=True)
    assert out.returncode != 0 or 'unparsed' in out.stdout


def test_readings_table():
    base = [x for x in ALL_OK]
    def r(bits, cls, idx=0):
        return dec.readings(dec.decode(code(bits, cls, idx)))
    # controls failed
    assert r([x for x in base if x != 0], [0] * 5) == ['controls_failed_readings_invalid']
    assert r([x for x in base if x != 1], [0] * 5) == ['controls_failed_readings_invalid']
    # R1
    assert r([x for x in base if x not in (2, 3, 4, 11, 12, 13)], [1, 1, 1, 1, 1])[0].startswith('R1 file_read_denied_error5')
    # R3 with S2,S3 ok, S4 error5, S8a error5, S8b fails
    got = r([x for x in base if x not in (4, 11, 12, 13)], [0, 0, 1, 1, 1])
    assert got[0] == 'R3 executable_mapping_failed_error5'
    # R4: S4 ok, S8a fails, S7 ok, S8b ok -> import order effect
    got = r([x for x in base if x != 11], [0, 0, 0, 1, 0])
    assert got[0].startswith('R4') and 'import_order_effect' in got[0]
    # R4 content: S8b fails
    got = r([x for x in base if x not in (11, 12)], [0, 0, 0, 1, 0])
    assert 'content_level' in got[0]
    # R6
    got = r([x for x in base if x not in (11, 12)], [0, 0, 0, 1, 0])
    assert 'R6 original_file_object_problem' in got


def test_pins_static_and_counts():
    stg.check_pins_static()
    assert len(stg.IMPORTS) == 19 and len([x for x in stg.IMPORTS if x.startswith('api-ms-win-')]) == 13
    assert stg.IMPORTS == dec.IMPORTS
    assert stg.c_import_names((D / 'disc90_probe.c').read_text()) == stg.IMPORTS
    bad = list(stg.IMPORTS)
    saved = stg.IMPORTS
    try:
        stg.IMPORTS = bad[:-1]
        with pytest.raises(ValueError):
            stg.check_pins_static()
    finally:
        stg.IMPORTS = saved
    assert stg.COPY_NAME == 'pydisc314.dll' and not re.match(r'python3[0-9]', stg.COPY_NAME)
    lock = json.loads((ROOT / 'verification/windows_pe_closure/EXPECTED-DIRECT-IMPORTS.json').read_text())['members']
    assert lock['python314.dll']['imports'] == stg.IMPORTS
    assert lock['python314.dll']['sha256'] == stg.RUNTIME_PINS['python314.dll']
    assert lock['vcruntime140.dll']['sha256'] == stg.RUNTIME_PINS['vcruntime140.dll']


def make_pe(names):
    """Minimal PE32+ with one section holding an import directory in the given order."""
    base = 0x1000
    dir_size = 20 * (len(names) + 1)
    blob = bytearray(dir_size)
    pos = dir_size
    for i, n in enumerate(names):
        raw = n.encode('ascii') + b'\0'
        struct.pack_into('<IIIII', blob, 20 * i, 0, 0, 0, base + pos, 0)
        blob += raw
        pos += len(raw)
    hdr = bytearray(0x400)
    hdr[0:2] = b'MZ'
    struct.pack_into('<I', hdr, 0x3c, 0x80)
    hdr[0x80:0x84] = b'PE\0\0'
    opt_size = 240
    struct.pack_into('<HHIIIHH', hdr, 0x84, 0x8664, 1, 0, 0, 0, opt_size, 0x2022)
    opt = 0x80 + 24
    struct.pack_into('<H', hdr, opt, 0x20b)
    struct.pack_into('<II', hdr, opt + 112 + 8, base, dir_size)
    sec = opt + opt_size
    hdr[sec:sec + 8] = b'.idata\0\0'
    struct.pack_into('<IIII', hdr, sec + 8, len(blob), base, len(blob), 0x400)
    return bytes(hdr) + bytes(blob)


def test_pe_import_order_and_refusals():
    names = ['B.dll', 'a.dll', 'C.dll']
    assert pe_imports.direct_imports(make_pe(names)) == names
    with pytest.raises(ValueError):
        pe_imports.direct_imports(b'notpe' * 100)


def synthetic_runtime(tmp_path, monkeypatch, imports=None, drop=None):
    runtime = tmp_path / 'acq'
    runtime.mkdir()
    py = make_pe(imports or stg.IMPORTS)
    files = {'python.exe': b'MZ' + b'0' * 300, 'python314.dll': py, 'vcruntime140.dll': b'v' * 50, 'libffi-8.dll': b'f' * 60,
             'python314.zip': b'z', 'python314._pth': b'p'}
    for n, b in files.items():
        if n != drop:
            (runtime / n).write_bytes(b)
    monkeypatch.setattr(stg, 'RUNTIME_PINS', {'python314.dll': stg.sha(py), 'vcruntime140.dll': stg.sha(files['vcruntime140.dll']),
                                              'libffi-8.dll': stg.sha(files['libffi-8.dll'])})
    return runtime


def test_verify_runtime_refusals(tmp_path, monkeypatch):
    runtime = synthetic_runtime(tmp_path, monkeypatch)
    before = stg.tree(runtime)
    src = D / 'disc90_probe.c'
    stg.verify_runtime(before, runtime, src)
    runtime2 = tmp_path / 'acq2'
    runtime2.mkdir()
    for n in before:
        (runtime2 / n).write_bytes((runtime / n).read_bytes())
    # libffi absent from the pinned list
    gone = dict(before)
    del gone['libffi-8.dll']
    with pytest.raises(ValueError):
        stg.verify_runtime(gone, runtime, src)
    # hash changed
    changed = dict(before)
    changed['libffi-8.dll'] = '0' * 64
    with pytest.raises(ValueError):
        stg.verify_runtime(changed, runtime, src)
    # import order differs
    swapped = list(stg.IMPORTS)
    swapped[0], swapped[1] = swapped[1], swapped[0]
    runtime3 = tmp_path / 'acq3'
    runtime3.mkdir()
    (runtime3 / 'python314.dll').write_bytes(make_pe(swapped))
    with pytest.raises(ValueError):
        stg.verify_runtime(before, runtime3, src)
    # copy name already there
    withcopy = dict(before)
    withcopy[stg.COPY_NAME] = 'x'
    with pytest.raises(ValueError):
        stg.verify_runtime(withcopy, runtime, src)
    # probe source list differs
    bad = tmp_path / 'bad.c'
    bad.write_text((D / 'disc90_probe.c').read_text().replace('L"bcrypt.dll"', 'L"bcrypt2.dll"'))
    with pytest.raises(ValueError):
        stg.verify_runtime(before, runtime, bad)


def test_derive_adds_exactly_one_pinned_copy(tmp_path, monkeypatch):
    runtime = synthetic_runtime(tmp_path, monkeypatch)
    owned = tmp_path / 'owned'
    owned.mkdir()
    (owned / 'ENTRY77-OWNER.json').write_text(json.dumps({'owner': 'o1', 'state': 'preparing-entry77'}))
    probe = tmp_path / 'probe.exe'
    probe.write_bytes(b'MZ' + b'1' * 200)
    derived, data = stg.derive(runtime, owned, 'o1', probe, stg.sha(probe.read_bytes()), D / 'disc90_probe.c')
    after = stg.tree(derived)
    before = stg.tree(runtime)
    assert set(after) - set(before) == {stg.COPY_NAME}
    assert after[stg.COPY_NAME] == stg.RUNTIME_PINS['python314.dll'] == before['python314.dll']
    assert after['python.exe'] == stg.sha(data) and after['libffi-8.dll'] == before['libffi-8.dll']
    with pytest.raises(ValueError):
        stg.derive(runtime, owned, 'o1', probe, stg.sha(probe.read_bytes()), D / 'disc90_probe.c')  # derived already exists
    with pytest.raises(ValueError):
        stg.derive(runtime, owned, 'wrong', probe, stg.sha(probe.read_bytes()), D / 'disc90_probe.c')


def test_probe_source_banned_and_kernel32_only():
    text = (D / 'disc90_probe.c').read_text()
    code_only = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    for banned in ('printf', 'WriteFile', 'GetStdHandle', 'OutputDebugString', 'WinHttp', 'URLDownload', 'socket', 'CreateProcess',
                   'ShellExecute', 'SetSecurityInfo', 'SetNamedSecurityInfo', 'SetFileSecurity', 'DeleteFile', 'MoveFile', 'CopyFile',
                   'CreateFileW(path, GENERIC_WRITE', 'ExitProcess', '#include <stdio', '#include <string', '#pragma comment'):
        assert banned not in code_only, banned
    assert code_only.count('TerminateProcess(') == 3  # two early refusals plus the result exit
    assert '#include <windows.h>' in text
    # advapi32 is only reached dynamically; the link line names kernel32 only
    wf = (ROOT / '.github/workflows/windows-disc90-probe-observation.yml').read_text()
    assert 'kernel32.lib' in wf and wf.count('advapi32.lib') == 1  # the unchanged launcher line only
    link = [l for l in wf.splitlines() if 'disc90_probe.c' in l and 'cl.exe' in l][0]
    assert 'advapi32' not in link and '/NODEFAULTLIB' in link
    # fixed order of steps in the source
    order = [text.index(x) for x in ('0x1u, 1, &cls); if (ok) bits |= 1u << 0', 'L"libffi-8.dll"', 'L"python314.dll", 0x2u', 'L"python314.dll", 0x20u',
                                     'L"python314.dll", 0x1u', 'acl_dump();', 'bits |= 1u << 11', 'for (k = 0; k < 19; k++)', 'bits |= 1u << 12', 'bits |= 1u << 13')]
    assert order == sorted(order)


def test_exit_layout_matches_decoder_and_fits_bits():
    text = (D / 'disc90_probe.c').read_text()
    exit_expr = re.search(r'TerminateProcess\(GetCurrentProcess\(\), 0x20000000u \| bits(.*?)\);', text, re.S).group(1)
    assert [int(x) for x in re.findall(r'<< (\d+)\)', exit_expr)] == [14, 16, 18, 20, 22, 24]
    assert [s for _n, _b, s in dec.CLASS_FIELDS] == [14, 16, 18, 20, 22]
    assert max(int(x) for x in re.findall(r'bits \|= 1u << (\d+)', text)) == 13


def test_workflow_equals_dependency_workflow_except_expected():
    a = (ROOT / '.github/workflows/windows-dependency-probe-observation.yml').read_text().splitlines()
    b = (ROOT / '.github/workflows/windows-disc90-probe-observation.yml').read_text().splitlines()
    import difflib
    changed = [l for l in difflib.unified_diff(a, b, lineterm='', n=0) if l[:1] in '+-' and l[:3] not in ('+++', '---')]
    joined = '\n'.join(changed)
    for forbidden in ('Invoke-WebRequest', 'Invoke-RestMethod', 'curl', 'wget', 'Set-ExecutionPolicy', 'Add-MpPreference', 'icacls', 'takeown',
                      'AllApplicationPackages', 'S-1-15-2-1', 'IFEO', 'reg add', 'DisableAntiSpyware'):
        assert forbidden not in joined
    # unchanged safety-critical lines (launcher, watchdog, acquisition, ACL qualify flow, allowlist, cleanup)
    for needle in ('launcher_nodebug_diag.c', 'outer_watchdog.c', 'acquire_locked.py', 'qualify_diagnostic.ps1', 'owner_guard.py',
                   '@(0,122,123,124,126)', 'timeout-minutes: 15', 'runs-on: windows-2022', 'persist-credentials: false'):
        assert (needle in '\n'.join(a)) == (needle in '\n'.join(b)) and needle in '\n'.join(b)
    assert 'Stage retention required; cleanup not confirmed' in '\n'.join(b)


def test_pins_file_matches_files():
    pins = json.loads((D / 'DISC90-SHA256.json').read_text())
    assert set(pins) == {'verification/windows_disc90_probe/' + n for n in ('disc90_probe.c', 'decode_disc90.py', 'stage_disc90.py', 'pe_imports.py')}
    for path, pin in pins.items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == pin


EMBED = Path('/tmp/embed.zip')


@pytest.mark.skipif(not EMBED.exists(), reason='pinned embed zip not available offline')
def test_real_embed_zip_pins():
    assert hashlib.sha256(EMBED.read_bytes()).hexdigest() == '90905e64a840557d8c1bc4f5b74db3d7ee85e3ffb217611cac1c7813acea4159'
    with zipfile.ZipFile(EMBED) as z:
        names = set(z.namelist())
        assert 'libffi-8.dll' in names
        for n, pin in stg.RUNTIME_PINS.items():
            assert hashlib.sha256(z.read(n)).hexdigest() == pin
        assert pe_imports.direct_imports(z.read('python314.dll')) == stg.IMPORTS


def test_event_log_step_is_read_only_bounded_and_exit_neutral():
    wf = (ROOT / '.github/workflows/windows-disc90-probe-observation.yml').read_text()
    start = wf.index('$runEnd=Get-Date')
    end = wf.index("Write-Output ('diag_nodebug_child_exit code='")
    step = wf[start:end]
    assert wf.index('$runStart=Get-Date') < wf.index('qualify_diagnostic.ps1" -Launcher')
    for log in ('Microsoft-Windows-CodeIntegrity/Operational', 'Microsoft-Windows-Windows Defender/Operational',
                'Microsoft-Windows-AppLocker/EXE and DLL', 'Microsoft-Windows-Security-Mitigations/*'):
        assert log in step
    assert step.count('Get-WinEvent') == 2 and 'try{' in step and 'catch{' in step
    assert '-ge 15' in step and 'Substring(0,160)' in step and r'\x20-\x7E' in step
    for banned in ('Set-', 'Clear-', 'Remove-', 'wevtutil', 'New-', 'Add-', 'Disable', 'Enable', 'sc.exe', 'reg ', 'exit ', 'throw', '$diagnosticExit', '$safeCleanup', '$LASTEXITCODE'):
        assert banned not in step, banned
    # window start is taken after safeCleanup=false and before the child, end after it
    assert wf.index('$safeCleanup=$false') < wf.index('$runStart=Get-Date')
