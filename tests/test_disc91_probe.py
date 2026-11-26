"""Static and logic checks of the disc91 diagnostic packet. Linux only: nothing here runs Windows code.
The C probe is NOT compiled or run here. Covers the decoder, PE patches and pins, staging refusals, workflow
equivalence and banned strings. Not acceptance."""
import hashlib
import json
from pathlib import Path
import re
import struct
import sys
import subprocess
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / 'verification/windows_disc91_probe'
sys.path.insert(0, str(D))
import decode_disc91 as dec  # noqa: E402
import pe_patch as pp  # noqa: E402
import stage_disc91 as stg  # noqa: E402

EMBED = Path('/tmp/embed.zip')
ALL_OK = list(range(9))


def code(bits=(), cls=None, nt=0):
    value = 0x20000000
    for b in bits:
        value |= 1 << b
    for (name, _b, shift), c in zip(dec.CLASS_FIELDS, cls or [0] * 7):
        value |= c << shift
    return value | (nt << 23)


def test_layout_matches_probe_source():
    text = (D / 'disc91_probe.c').read_text()
    exit_expr = re.search(r'TerminateProcess\(GetCurrentProcess\(\), 0x20000000u \| bits(.*?)\);', text, re.S).group(1)
    assert [int(x) for x in re.findall(r'<< (\d+)\)', exit_expr)] == [9, 11, 13, 15, 17, 19, 21, 23]
    assert [s for _n, _b, s in dec.CLASS_FIELDS] == [9, 11, 13, 15, 17, 19, 21]
    assert sorted(int(x) for x in re.findall(r'load_abs\(L"[^"]+", \w+, \d, (\d+), (-?\d+)\)', text) and
                  [m[0] for m in re.findall(r'load_abs\(L"[^"]+", \w+, \d, (\d+), (-?\d+)\)', text)]) == list(range(9))
    slots = {name: (int(bit), int(slot)) for name, bit, slot in
             re.findall(r'load_abs\(L"([^"]+)", \w+, \d, (\d+), (-?\d+)\);', text)[2:]}
    assert slots['pydisc91_v1.dll'] == (4, 2) and slots['pydisc91_v2.dll'] == (5, 3)
    assert slots['pydisc91_v3.dll'] == (6, 4) and slots['pydisc91_v4.dll'] == (7, 5) and slots['pydisc91_v0.dll'] == (3, 1)
    for name, bit, shift in dec.CLASS_FIELDS:
        assert shift == 9 + 2 * [n for n, _b, _s in dec.CLASS_FIELDS].index(name)


def test_all_ok_and_each_class():
    d = dec.decode(code(ALL_OK))
    assert d['bits'] == [1] * 9 and d['nt'] == 0
    for i, (name, b, _s) in enumerate(dec.CLASS_FIELDS):
        for c in (1, 2, 3):
            cls = [0] * 7
            cls[i] = c
            assert dec.decode(code([x for x in ALL_OK if x != b], cls))['classes'][name] == c
    for nt in range(5):
        assert dec.decode(code(ALL_OK, nt=nt))['nt'] == nt


@pytest.mark.parametrize('value', [
    0, 5, 3221225506, 0x40000000, 0x80000000, 0x40000001,
    code(ALL_OK, nt=5), code(ALL_OK, nt=7),
    code([x for x in ALL_OK if x != 3]),  # failed V0 without a class
    code(ALL_OK, [0, 1, 0, 0, 0, 0, 0]),  # class without failure
    code(ALL_OK) | (1 << 26), code(ALL_OK) | (1 << 28),
])
def test_non_parsing_refused(value):
    with pytest.raises(ValueError):
        dec.decode(value)


def test_exact_marker_only_is_refusal_not_result():
    assert dec.decode(0x20000000) == {'refused': True}


def test_cli_refuses_and_prints_raw():
    out = subprocess.run([sys.executable, '-B', str(D / 'decode_disc91.py'), '--exit', '542115583'], capture_output=True, text=True)
    assert out.returncode == 2 and 'disc91_decode_refused raw_exit=542115583' in out.stdout


def reading(failed, cls_extra=None):
    bits = [x for x in ALL_OK if x not in failed]
    cls = [0] * 7
    for i, (_n, b, _s) in enumerate(dec.CLASS_FIELDS):
        if b in failed:
            cls[i] = 1
    return dec.readings(dec.decode(code(bits, cls)))


def test_reading_table():
    base = [2, 3, 4, 5, 6, 7, 8]  # S8a V0 V1 V2 V3 V4 S8b all failing
    assert reading([0]) == ['controls_failed_readings_invalid']
    assert reading([1]) == ['controls_failed_readings_invalid']
    assert reading([3]) == ['baseline_not_reproduced_readings_invalid']
    assert reading([x for x in base if x != 4]) == ['V1_tls_directory_zeroed_loads', 'V4_fails_none_of_the_three_alone_or_together']
    r = reading([2, 3, 4, 5, 6, 8])  # V4 passes, singles fail
    assert r == ['V4_combination_loads_while_singles_fail']
    assert reading(base) == ['V4_fails_none_of_the_three_alone_or_together']
    assert reading([2, 4, 5, 6, 7, 8])[0].startswith('V0_passed')
    assert reading([x for x in base if x != 6])[0] == 'V3_entry_stub_loads'
    assert reading([x for x in base if x != 5])[0] == 'V2_resource_directory_zeroed_loads'
    assert reading([3, 4, 5, 6, 7, 8]) == ['baseline_not_reproduced_readings_invalid']


def test_rules_supersede_old_line_and_limits():
    assert not any(r.startswith('no_further_instrument_after_this_run') for r in dec.RULES)
    assert any('superseded_by_per_run_approval' in r for r in dec.RULES)
    out = subprocess.run([sys.executable, '-B', str(D / 'decode_disc91.py'), '--exit', str(code([x for x in ALL_OK if x != 2], [1, 0, 0, 0, 0, 0, 0], 1))], capture_output=True, text=True)
    assert 'security_acceptance=NOT_ESTABLISHED' in out.stdout and 'disc91_ldr_ntstatus class=C0000022_access_denied' in out.stdout


def fake_dll():
    """Small PE32+ with the same header field offsets and a .text section, so patches can be exercised offline."""
    data = bytearray(0x2000)
    data[0:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    opt = 0x80 + 24
    struct.pack_into('<H', data, opt, 0x20b)
    return data, opt


def test_patches_touch_only_the_intended_bytes_on_the_real_dll():
    if not EMBED.exists():
        pytest.skip('pinned embed zip not available offline')
    with zipfile.ZipFile(EMBED) as z:
        original = z.read('python314.dll')
    assert hashlib.sha256(original).hexdigest() == pp.ORIGINAL_SHA256
    opt, off = pp.verify_facts(original)
    expected = {
        'v0': [(opt + 64, 4)],
        'v1': [(opt + 64, 4), (opt + 112 + 72, 8)],
        'v2': [(opt + 64, 4), (opt + 112 + 16, 8)],
        'v3': [(opt + 64, 4), (off, 6)],
        'v4': [(opt + 64, 4), (opt + 112 + 16, 8), (opt + 112 + 72, 8), (off, 6)],
    }
    for key in pp.VARIANTS:
        blob = pp.make(original, key)
        assert len(blob) == len(original) and hashlib.sha256(blob).hexdigest() == stg.VARIANT_PINS[key]
        covered = set()
        for a, n in expected[key]:
            covered.update(range(a, a + n))
        assert all(i in covered for a, n in pp.changed_ranges(original, blob) for i in range(a, a + n))
        assert struct.unpack_from('<I', blob, opt + 64)[0] == 0
    v4 = pp.make(original, 'v4')
    assert struct.unpack_from('<II', v4, opt + 112 + 72) == (0, 0) and struct.unpack_from('<II', v4, opt + 112 + 16) == (0, 0)
    assert v4[off:off + 6].hex() == 'b801000000c3' and struct.unpack_from('<I', v4, opt + 16)[0] == 0x1e8934
    v0 = pp.make(original, 'v0')
    assert struct.unpack_from('<II', v0, opt + 112 + 72) == (0x525f00, 0x28) and v0[off:off + 6] == original[off:off + 6]
    for key, other in (('v1', 'v2'), ('v1', 'v3'), ('v2', 'v3')):
        assert pp.make(original, key) != pp.make(original, other)
    # pinned facts equal the reviewer's design numbers
    assert pp.PE_FACTS['tls'] == (0x525F00, 0x28) and pp.PE_FACTS['resource'] == (0x6C3000, 0xA30)
    assert pp.PE_FACTS['entry'] == 0x1E8934 and pp.PE_FACTS['loadcfg'][0] == 0x3B2FA0


def test_verify_facts_refuses_other_files():
    data, _opt = fake_dll()
    with pytest.raises(ValueError):
        pp.verify_facts(bytes(data))
    with pytest.raises(ValueError):
        pp.make(bytes(data), 'v0')
    with pytest.raises(ValueError):
        pp.make(bytes(data), 'v9')


def test_pins_and_names():
    stg.check_pins_static()
    assert len(set(stg.VARIANT_PINS.values())) == 5 and set(pp.NAMES) == {'v0', 'v1', 'v2', 'v3', 'v4'}
    assert all(not re.match(r'python3[0-9]', n) for n in pp.NAMES.values())
    text = (D / 'disc91_probe.c').read_text()
    for n in pp.NAMES.values():
        assert text.count('L"%s"' % n) == 1
    lock = json.loads((ROOT / 'verification/windows_pe_closure/EXPECTED-DIRECT-IMPORTS.json').read_text())['members']
    assert lock['python314.dll']['sha256'] == stg.RUNTIME_PINS['python314.dll'] == pp.ORIGINAL_SHA256


@pytest.mark.skipif(not EMBED.exists(), reason='pinned embed zip not available offline')
def test_derive_on_real_runtime_adds_exactly_five_pinned_variants(tmp_path):
    runtime = tmp_path / 'acq'
    with zipfile.ZipFile(EMBED) as z:
        z.extractall(runtime)
    owned = tmp_path / 'owned'
    owned.mkdir()
    (owned / 'ENTRY77-OWNER.json').write_text(json.dumps({'owner': 'o1', 'state': 'preparing-entry77'}))
    probe = tmp_path / 'probe.exe'
    probe.write_bytes(b'MZ' + b'1' * 200)
    before = stg.tree(runtime)
    derived, data = stg.derive(runtime, owned, 'o1', probe, stg.sha(probe.read_bytes()), D / 'disc91_probe.c')
    after = stg.tree(derived)
    assert set(after) - set(before) == set(pp.NAMES.values())
    for key, name in pp.NAMES.items():
        assert after[name] == stg.VARIANT_PINS[key]
    assert after['python314.dll'] == before['python314.dll'] and after['python.exe'] == stg.sha(data)
    assert stg.tree(runtime) == before
    with pytest.raises(ValueError):
        stg.derive(runtime, owned, 'wrong', probe, stg.sha(probe.read_bytes()), D / 'disc91_probe.c')


def test_verify_runtime_refusals(tmp_path):
    if not EMBED.exists():
        pytest.skip('pinned embed zip not available offline')
    runtime = tmp_path / 'acq'
    with zipfile.ZipFile(EMBED) as z:
        z.extractall(runtime)
    before = stg.tree(runtime)
    stg.verify_runtime(before, runtime, D / 'disc91_probe.c')
    for drop in ('libffi-8.dll', 'vcruntime140.dll', 'python314.dll'):
        gone = dict(before)
        del gone[drop]
        with pytest.raises(ValueError):
            stg.verify_runtime(gone, runtime, D / 'disc91_probe.c')
    changed = dict(before)
    changed['libffi-8.dll'] = '0' * 64
    with pytest.raises(ValueError):
        stg.verify_runtime(changed, runtime, D / 'disc91_probe.c')
    withname = dict(before)
    withname['pydisc91_v1.dll'] = 'x'
    with pytest.raises(ValueError):
        stg.verify_runtime(withname, runtime, D / 'disc91_probe.c')
    bad = tmp_path / 'bad.c'
    bad.write_text((D / 'disc91_probe.c').read_text().replace('pydisc91_v2.dll', 'pydisc91_vX.dll'))
    with pytest.raises(ValueError):
        stg.verify_runtime(before, runtime, bad)


def test_probe_source_banned_and_kernel32_only():
    text = (D / 'disc91_probe.c').read_text()
    code_only = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    for banned in ('printf', 'WriteFile', 'GetStdHandle', 'OutputDebugString', 'WinHttp', 'URLDownload', 'socket', 'CreateProcess',
                   'ShellExecute', 'SetSecurityInfo', 'SetNamedSecurityInfo', 'SetFileSecurity', 'DeleteFile', 'MoveFile', 'CopyFile',
                   'ExitProcess', '#include <stdio', '#include <string', '#pragma comment', 'NtCreate', 'NtOpen', 'LoadLibraryW'):
        assert banned not in code_only, banned
    assert code_only.count('TerminateProcess(') == 3
    assert text.count('LdrLoadDll') >= 1 and 'GetProcAddress(nt, "LdrLoadDll")' in text
    wf = (ROOT / '.github/workflows/windows-disc91-probe-observation.yml').read_text()
    link = [l for l in wf.splitlines() if 'disc91_probe.c' in l and 'cl.exe' in l][0]
    assert 'kernel32.lib' in link and 'advapi32' not in link and '/NODEFAULTLIB' in link
    order = [text.index(x) for x in ('L"vcruntime140.dll"', 'L"libffi-8.dll"', 'L"python314.dll", 0u, 0, 2, 0', 'ldr_baseline();', 'pydisc91_v0.dll"',
                                     'pydisc91_v3.dll"', 'pydisc91_v4.dll"', 'pydisc91_v2.dll"', 'L"python314.dll", 0u, 0, 8, 6', 'pydisc91_v1.dll"')]
    assert order == sorted(order)


def test_workflow_equals_disc90_workflow_except_names():
    a = (ROOT / '.github/workflows/windows-disc90-probe-observation.yml').read_text().replace('disc90', 'disc91').replace('Disc90', 'Disc91')
    a = a.replace('one_pinned_byte_copy_of_python314_dll_added=1', 'five_pinned_patched_copies_of_python314_dll_added=1')
    assert a == (ROOT / '.github/workflows/windows-disc91-probe-observation.yml').read_text()
    for forbidden in ('Invoke-WebRequest', 'Invoke-RestMethod', 'curl', 'wget', 'Set-ExecutionPolicy', 'Add-MpPreference', 'icacls', 'takeown',
                      'AllApplicationPackages', 'S-1-15-2-1', 'IFEO', 'reg add', 'DisableAntiSpyware'):
        assert forbidden not in a


def test_pins_file_matches_files():
    pins = json.loads((D / 'DISC91-SHA256.json').read_text())
    assert set(pins) == {'verification/windows_disc91_probe/' + n for n in ('disc91_probe.c', 'decode_disc91.py', 'stage_disc91.py', 'pe_patch.py')}
    for path, pin in pins.items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == pin
