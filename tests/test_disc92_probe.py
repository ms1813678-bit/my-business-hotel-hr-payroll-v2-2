"""Static and logic checks of the disc92 diagnostic packet. Linux only: nothing here runs Windows code.
The C probe is NOT compiled or run here. Covers the decoder, PE patches and pins, staging refusals, workflow
equivalence, prelaunch name and file checks. Not acceptance."""
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
D = ROOT / 'verification/windows_disc92_probe'
sys.path.insert(0, str(D))
import decode_disc92 as dec  # noqa: E402
import pe_patch92 as pp  # noqa: E402
import stage_disc92 as stg  # noqa: E402

EMBED = Path('/tmp/embed.zip')
ALL_OK = list(range(8))
NCLS = len(dec.CLASS_FIELDS)


def code(bits=(), cls=None, nt=0):
    value = 0x20000000
    for b in bits:
        value |= 1 << b
    for (_name, _b, shift), c in zip(dec.CLASS_FIELDS, cls or [0] * NCLS):
        value |= c << shift
    return value | (nt << 20)


def test_layout_matches_probe_source():
    text = (D / 'disc92_probe.c').read_text()
    exit_expr = re.search(r'TerminateProcess\(GetCurrentProcess\(\), 0x20000000u \| bits(.*?)\);', text, re.S).group(1)
    assert [int(x) for x in re.findall(r'<< (\d+)\)', exit_expr)] == [8, 10, 12, 14, 16, 18, 20]
    assert [s for _n, _b, s in dec.CLASS_FIELDS] == [8, 10, 12, 14, 16, 18]
    loads = re.findall(r'load_abs\(L"([^"]+)", \w+, \d, (\d+), (-?\d+)\);', text)
    assert sorted(int(b) for _n, b, _s in loads) == list(range(8))
    slots = {name: (int(bit), int(slot)) for name, bit, slot in loads[2:]}
    assert slots == {'python314.dll': (7, 5), 'pydisc92_v0.dll': (3, 1), 'pydisc92_b.dll': (5, 3),
                     'pydisc92_c.dll': (6, 4), 'pydisc92_a.dll': (4, 2)}
    names = [n for n, _b, _s in dec.CLASS_FIELDS]
    assert names == ['S8a', 'V0', 'A', 'B', 'C', 'S8b']
    assert [b for _n, b, _s in dec.CLASS_FIELDS] == [2, 3, 4, 5, 6, 7]
    assert 'static unsigned int cls[6]' in text and 'k < 6' in text


def test_all_ok_and_each_class():
    d = dec.decode(code(ALL_OK))
    assert d['bits'] == [1] * 8 and d['nt'] == 0
    for i, (name, b, _s) in enumerate(dec.CLASS_FIELDS):
        for c in (1, 2, 3):
            cls = [0] * NCLS
            cls[i] = c
            assert dec.decode(code([x for x in ALL_OK if x != b], cls))['classes'][name] == c
    for nt in range(5):
        assert dec.decode(code(ALL_OK, nt=nt))['nt'] == nt


@pytest.mark.parametrize('value', [
    0, 5, 3221225506, 0x40000000, 0x80000000, 0x40000001,
    code(ALL_OK, nt=5), code(ALL_OK, nt=7),
    code([x for x in ALL_OK if x != 3]),
    code(ALL_OK, [0, 1, 0, 0, 0, 0]),
    code(ALL_OK) | (1 << 23), code(ALL_OK) | (1 << 28),
])
def test_non_parsing_refused(value):
    with pytest.raises(ValueError):
        dec.decode(value)


def test_exact_marker_only_is_refusal_not_result():
    assert dec.decode(0x20000000) == {'refused': True}


def test_cli_refuses_and_prints_raw():
    out = subprocess.run([sys.executable, '-B', str(D / 'decode_disc92.py'), '--exit', '542115583'], capture_output=True, text=True)
    assert out.returncode == 2 and 'disc92_decode_refused raw_exit=542115583' in out.stdout


def reading(failed):
    bits = [x for x in ALL_OK if x not in failed]
    cls = [1 if b in failed else 0 for _n, b, _s in dec.CLASS_FIELDS]
    return dec.readings(dec.decode(code(bits, cls)))


def test_reading_table():
    base = [2, 3, 4, 5, 6, 7]  # S8a V0 A B C S8b failing
    assert reading([0]) == ['controls_failed_readings_invalid']
    assert reading([1]) == ['controls_failed_readings_invalid']
    assert reading([3]) == ['baseline_not_reproduced_readings_invalid']
    assert reading([2, 4, 5, 6, 7])[0].startswith('V0_passed')
    assert reading([x for x in base if x != 4]) == ['A_manifest_dependency_element_blanked_loads_B_failed_dependency_element_needed', 'C_fails']
    assert reading([x for x in base if x != 6]) == ['C_version_resource_zeroed_loads', 'A_fails']
    assert reading([x for x in base if x not in (4, 6)])[0] == 'A_manifest_dependency_element_blanked_loads_B_failed_dependency_element_needed'
    assert reading(base) == ['A_and_C_fail_neither_explains_stop_return_to_owner']
    assert reading([x for x in base if x != 5]) == ['B_control_loads_A_not_interpretable']
    assert reading([x for x in base if x not in (5, 4)]) == ['B_control_loads_A_not_interpretable']
    assert reading([x for x in base if x not in (5, 6)]) == ['B_control_loads_A_not_interpretable', 'C_version_zeroed_loads']


def test_rules_and_limits_and_no_authority_claims():
    assert not any('approval' in r or 'approved' in r or 'owner' in r.replace('return_to_owner', '') for r in dec.RULES)
    assert any('stop_and_return_to_owner' in r for r in dec.RULES)
    out = subprocess.run([sys.executable, '-B', str(D / 'decode_disc92.py'), '--exit', str(code([x for x in ALL_OK if x != 2], [1, 0, 0, 0, 0, 0], 1))], capture_output=True, text=True)
    assert 'security_acceptance=NOT_ESTABLISHED' in out.stdout and 'disc92_ldr_ntstatus class=C0000022_access_denied' in out.stdout


def fake_dll():
    data = bytearray(0x2000)
    data[0:2] = b'MZ'
    struct.pack_into('<I', data, 0x3c, 0x80)
    data[0x80:0x84] = b'PE\0\0'
    struct.pack_into('<H', data, 0x80 + 24, 0x20b)
    return data


def original():
    with zipfile.ZipFile(EMBED) as z:
        return z.read('python314.dll')


needs_embed = pytest.mark.skipif(not EMBED.exists(), reason='pinned embed zip not available offline')


@needs_embed
def test_patches_touch_only_the_intended_bytes_on_the_real_dll():
    data = original()
    assert hashlib.sha256(data).hexdigest() == pp.ORIGINAL_SHA256
    opt = pp.verify_facts(data)
    mo, ml = pp.PE_FACTS['manifest']
    vo, vl = pp.PE_FACTS['version']
    blobs = {k: pp.make(data, k) for k in pp.VARIANTS}
    for key, blob in blobs.items():
        assert len(blob) == len(data) and hashlib.sha256(blob).hexdigest() == stg.VARIANT_PINS[key]
        assert struct.unpack_from('<I', blob, opt + 64)[0] == 0
        # resource directory entry, entry point, TLS entry all intact
        assert struct.unpack_from('<II', blob, opt + 112 + 16) == pp.PE_FACTS['resource']
        assert struct.unpack_from('<I', blob, opt + 16)[0] == pp.PE_FACTS['entry']
        assert struct.unpack_from('<II', blob, opt + 112 + 72) == struct.unpack_from('<II', data, opt + 112 + 72)
    def changed(key):
        return {i for a, n in pp.changed_ranges(data, blobs[key]) for i in range(a, a + n)} - set(range(opt + 64, opt + 68))
    assert changed('v0') == set()
    ma = bytes(blobs['a'][mo:mo + ml])
    s, e = pp.dep_span(data[mo:mo + ml])
    assert changed('a') <= set(range(mo + s, mo + e)) and ma[s:e] == b' ' * (e - s)
    assert b'Common-Controls' not in ma and b'dependency' not in ma and ma[:s] == data[mo:mo + s] and ma[e:] == data[mo + e:mo + ml]
    mb = bytes(blobs['b'][mo:mo + ml])
    assert len(changed('b')) == 1 and mb.count(b'Vistb') == 1 and mb.replace(b'Vistb', b'Vista') == data[mo:mo + ml]
    assert changed('c') <= set(range(vo, vo + vl)) and blobs['c'][vo:vo + vl] == bytes(vl)
    assert changed('c') and all(i not in range(mo, mo + ml) for i in changed('c'))
    assert all(i not in range(vo, vo + vl) for k in ('a', 'b') for i in changed(k))
    # RT_VERSION 940 B and RT_MANIFEST 1499 B, as the reviewer's design says
    assert (ml, vl) == (1499, 940)
    for x, y in (('a', 'b'), ('a', 'c'), ('b', 'c'), ('v0', 'a')):
        assert blobs[x] != blobs[y]


@needs_embed
def test_manifest_a_is_valid_xml_and_b_is_valid_xml():
    import xml.dom.minidom as md
    data = original()
    mo, ml = pp.PE_FACTS['manifest']
    for k in ('v0', 'a', 'b'):
        md.parseString(pp.make(data, k)[mo:mo + ml])
    m = md.parseString(pp.make(data, 'a')[mo:mo + ml])
    assert not m.getElementsByTagName('dependency') and m.getElementsByTagName('compatibility')


@needs_embed
def test_resource_directory_rva_matches_pins():
    data = original()
    # locate RT_VERSION and RT_MANIFEST again from the PE resource tree and compare to the pinned file offsets
    pe = struct.unpack_from('<I', data, 0x3c)[0]
    opt = pe + 24
    nsec = struct.unpack_from('<H', data, pe + 6)[0]
    osz = struct.unpack_from('<H', data, pe + 20)[0]
    secs = []
    for i in range(nsec):
        o = opt + osz + i * 40
        vs, va, rs, rp = struct.unpack_from('<IIII', data, o + 8)
        secs.append((va, max(vs, rs), rp))
    def r2o(r):
        return [r - va + rp for va, sz, rp in secs if va <= r < va + sz][0]
    rva, _size = struct.unpack_from('<II', data, opt + 112 + 16)
    base = r2o(rva)
    def ents(off):
        nn, ni = struct.unpack_from('<HH', data, off + 12)
        return [struct.unpack_from('<II', data, off + 16 + i * 8) for i in range(nn + ni)]
    found = {}
    for t, o in ents(base):
        for nm, o2 in ents(base + (o & 0x7fffffff)):
            for _lg, o3 in ents(base + (o2 & 0x7fffffff)):
                dr, sz, _cp, _r = struct.unpack_from('<IIII', data, base + o3)
                found[(t, nm)] = (r2o(dr), sz)
    assert found[(16, 1)] == pp.PE_FACTS['version'] and found[(24, 2)] == pp.PE_FACTS['manifest']


def test_verify_facts_refuses_other_files():
    data = fake_dll()
    with pytest.raises(ValueError):
        pp.verify_facts(bytes(data))
    with pytest.raises(ValueError):
        pp.make(bytes(data), 'v0')
    with pytest.raises(ValueError):
        pp.make(bytes(data), 'v9')


@needs_embed
def test_make_refuses_changed_manifest_or_version_bytes():
    data = bytearray(original())
    mo, _ml = pp.PE_FACTS['manifest']
    data[mo + 100] ^= 1
    with pytest.raises(ValueError):
        pp.make(bytes(data), 'a')


def test_pins_and_names():
    stg.check_pins_static()
    assert len(set(stg.VARIANT_PINS.values())) == 4 and set(pp.NAMES) == {'v0', 'a', 'b', 'c'}
    assert all(not re.match(r'python3[0-9]', n) for n in pp.NAMES.values())
    text = (D / 'disc92_probe.c').read_text()
    for n in pp.NAMES.values():
        assert text.count('L"%s"' % n) == 1
    lock = json.loads((ROOT / 'verification/windows_pe_closure/EXPECTED-DIRECT-IMPORTS.json').read_text())['members']
    assert lock['python314.dll']['sha256'] == stg.RUNTIME_PINS['python314.dll'] == pp.ORIGINAL_SHA256


@needs_embed
def test_derive_on_real_runtime_adds_exactly_four_pinned_variants(tmp_path):
    runtime = tmp_path / 'acq'
    with zipfile.ZipFile(EMBED) as z:
        z.extractall(runtime)
    owned = tmp_path / 'owned'
    owned.mkdir()
    (owned / 'ENTRY77-OWNER.json').write_text(json.dumps({'owner': 'o1', 'state': 'preparing-entry77'}))
    probe = tmp_path / 'probe.exe'
    probe.write_bytes(b'MZ' + b'1' * 200)
    before = stg.tree(runtime)
    derived, data = stg.derive(runtime, owned, 'o1', probe, stg.sha(probe.read_bytes()), D / 'disc92_probe.c')
    after = stg.tree(derived)
    assert set(after) - set(before) == set(pp.NAMES.values())
    for key, name in pp.NAMES.items():
        assert after[name] == stg.VARIANT_PINS[key]
    assert after['python314.dll'] == before['python314.dll'] and after['python.exe'] == stg.sha(data)
    assert stg.tree(runtime) == before
    with pytest.raises(ValueError):
        stg.derive(runtime, owned, 'wrong', probe, stg.sha(probe.read_bytes()), D / 'disc92_probe.c')


@needs_embed
def test_verify_runtime_refusals(tmp_path):
    runtime = tmp_path / 'acq'
    with zipfile.ZipFile(EMBED) as z:
        z.extractall(runtime)
    before = stg.tree(runtime)
    stg.verify_runtime(before, runtime, D / 'disc92_probe.c')
    for drop in ('libffi-8.dll', 'vcruntime140.dll', 'python314.dll'):
        gone = dict(before)
        del gone[drop]
        with pytest.raises(ValueError):
            stg.verify_runtime(gone, runtime, D / 'disc92_probe.c')
    changed = dict(before)
    changed['libffi-8.dll'] = '0' * 64
    with pytest.raises(ValueError):
        stg.verify_runtime(changed, runtime, D / 'disc92_probe.c')
    withname = dict(before)
    withname['pydisc92_a.dll'] = 'x'
    with pytest.raises(ValueError):
        stg.verify_runtime(withname, runtime, D / 'disc92_probe.c')
    bad = tmp_path / 'bad.c'
    bad.write_text((D / 'disc92_probe.c').read_text().replace('pydisc92_c.dll', 'pydisc92_X.dll'))
    with pytest.raises(ValueError):
        stg.verify_runtime(before, runtime, bad)


def test_probe_source_banned_and_kernel32_only():
    text = (D / 'disc92_probe.c').read_text()
    code_only = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    for banned in ('printf', 'WriteFile', 'GetStdHandle', 'OutputDebugString', 'WinHttp', 'URLDownload', 'socket', 'CreateProcess',
                   'ShellExecute', 'SetSecurityInfo', 'SetNamedSecurityInfo', 'SetFileSecurity', 'DeleteFile', 'MoveFile', 'CopyFile',
                   'ExitProcess', '#include <stdio', '#include <string', '#pragma comment', 'NtCreate', 'NtOpen', 'LoadLibraryW'):
        assert banned not in code_only, banned
    assert code_only.count('TerminateProcess(') == 3
    assert 'GetProcAddress(nt, "LdrLoadDll")' in text
    wf = (ROOT / '.github/workflows/windows-disc92-probe-observation.yml').read_text()
    link = [l for l in wf.splitlines() if 'disc92_probe.c' in l and 'cl.exe' in l][0]
    assert 'kernel32.lib' in link and 'advapi32' not in link and '/NODEFAULTLIB' in link
    order = [text.index(x) for x in ('L"vcruntime140.dll"', 'L"libffi-8.dll"', 'L"python314.dll", 0u, 0, 2, 0', 'ldr_baseline();', 'pydisc92_v0.dll"',
                                     'pydisc92_b.dll"', 'L"python314.dll", 0u, 0, 7, 5', 'pydisc92_c.dll"', 'pydisc92_a.dll"')]
    assert order == sorted(order)


def test_workflow_equals_disc91_workflow_except_names():
    a = (ROOT / '.github/workflows/windows-disc91-probe-observation.yml').read_text().replace('disc91', 'disc92').replace('Disc91', 'Disc92').replace('DISC91', 'DISC92')
    a = a.replace('five_pinned_patched_copies_of_python314_dll_added=1', 'four_pinned_patched_copies_of_python314_dll_added=1')
    assert a == (ROOT / '.github/workflows/windows-disc92-probe-observation.yml').read_text()
    for forbidden in ('Invoke-WebRequest', 'Invoke-RestMethod', 'curl', 'wget', 'Set-ExecutionPolicy', 'Add-MpPreference', 'icacls', 'takeown',
                      'AllApplicationPackages', 'S-1-15-2-1', 'IFEO', 'reg add', 'DisableAntiSpyware'):
        assert forbidden not in a


def test_pins_file_matches_files():
    pins = json.loads((D / 'DISC92-SHA256.json').read_text())
    assert set(pins) == {'verification/windows_disc92_probe/' + n for n in ('disc92_probe.c', 'decode_disc92.py', 'stage_disc92.py', 'pe_patch92.py')}
    for path, pin in pins.items():
        assert hashlib.sha256((ROOT / path).read_bytes()).hexdigest() == pin


def test_prelaunch_no_older_names_and_every_referenced_file_exists():
    wf = (ROOT / '.github/workflows/windows-disc92-probe-observation.yml').read_text()
    assert not re.search(r'disc9[01]|DISC9[01]', wf, re.I)
    for f in D.iterdir():
        if f.suffix in ('.c', '.py', '.json'):
            assert not re.search(r'disc9[01]', f.read_text(), re.I), f.name
    names = set(re.findall(r'\$probeDir/([A-Za-z0-9_.-]+)', wf)) | set(re.findall(r'"\$probeDir/([A-Za-z0-9_.-]+)"', wf))
    assert names
    for name in names:
        assert (D / name).is_file(), name
    assert 'DISC92-SHA256.json' in wf and 'windows_disc92_probe' in wf
    assert 'windows-disc92-probe-observation.yml' in wf
    assert sorted(p.name for p in D.iterdir() if p.name != '__pycache__') == ['DISC92-SHA256.json', 'decode_disc92.py', 'disc92_probe.c', 'pe_patch92.py', 'stage_disc92.py']
