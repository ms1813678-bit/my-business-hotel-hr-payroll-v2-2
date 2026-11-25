import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
D = ROOT / "verification/windows_integrated_candidate"
PROBE = (D / "probe_load.c").read_text()
DRIVER = (D / "run_probe.py").read_text()
QUALIFY = (D / "qualify_runtime.ps1").read_text()
WF_PATH = ROOT / ".github/workflows/windows-integrated-loader-probe.yml"
WF = WF_PATH.read_text()
BASE = (ROOT / ".github/workflows/windows-integrated-candidate.yml").read_text()


def test_workflow_is_valid_yaml_with_least_privilege_and_own_trigger():
    # No YAML library is a repo dependency: guard the failure class that broke the OFF control
    # (a plain scalar containing ": ") and the structure by text. A real parse runs in review.
    for line in WF.splitlines():
        m = re.match(r"\s*(?:- )?name:\s*(.*)$", line)
        if m and not m.group(1).startswith(('"', "'")):
            assert ": " not in m.group(1) and " #" not in m.group(1), line
    assert "permissions:\n  contents: read" in WF
    assert WF.count("      - name:") == 1 and "continue-on-error" not in WF
    assert "paths: [.github/workflows/windows-integrated-loader-probe.yml]" in WF
    assert "NOT_ESTABLISHED" in WF and "Invoke-WithLoaderSnaps" not in WF


def test_workflow_keeps_baseline_gates_before_the_probe():
    for needle in ("Committed source hash mismatch", "Runtime signature verification failed",
                   "Pinned runtime startup/version failed", "Private runtime staging failed",
                   "/W4 /WX", "-Probe"):
        assert needle in WF, needle
    assert "ALL_APPLICATION_PACKAGES" not in WF


def test_probe_is_kernel32_only_and_prints_no_text_from_the_system():
    assert "#include <windows.h>" in PROBE
    for banned in ("stdio.h", "printf", "wsprintf", "getenv", "CreateProcess", "socket", "WSAStartup",
                   "RegOpen", "CreateFile", "SetSecurity", "Adjust"):
        assert banned not in PROBE, banned
    assert "GetModuleFileName" in PROBE and "LOAD_LIBRARY_SEARCH_SYSTEM32" in PROBE
    labels = re.findall(r'try_(?:system|staged)\("([A-Za-z0-9_-]+)"', PROBE)
    assert "ws2_32" in labels and "vcruntime140" in labels
    assert labels.index("control_dll") < labels.index("python314") < labels.index("python314_noresolve") < labels.index("python314_imageres")
    assert labels[-1] == "python314_imageres"
    assert "DONT_RESOLVE_DLL_REFERENCES" in PROBE and "LOAD_LIBRARY_AS_IMAGE_RESOURCE" in PROBE


def test_driver_accepts_only_fixed_vocabulary_probe_lines():
    LINE = _driver().CHILD_LINE
    assert LINE.match("probe_load label=ws2_32 ok=0 error=5")
    assert LINE.match("probe_start ok=1")
    assert not LINE.match("probe_load label=C:\\Users\\x ok=1")
    assert not LINE.match("python.exe failed")
    assert "NOT_ESTABLISHED" in DRIVER


def test_default_qualification_path_is_unchanged_without_probe_switch():
    assert "[switch]$Probe" in QUALIFY
    assert "run_request.py" in QUALIFY and "Contained real parser qualification refused" in QUALIFY
    assert "run_probe.py" in QUALIFY


def _driver():
    import importlib.util
    spec = importlib.util.spec_from_file_location("run_probe_under_test", D / "run_probe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


GOOD = b"probe_start ok=1\nprobe_load label=ws2_32 ok=1 error=0\nprobe_load label=python314 ok=0 error=126\nprobe_done ok=1\n"
NATIVE = (b"CANDIDATE_CREATED pid=1 suspended=1\nloader_debug_module name=ntdll.dll clamped=0 dir=system32\n"
          b"loader_debug_summary loads=1 last_module=ntdll.dll\nC:\\secret\\path leaked\nCANDIDATE_CLEANUP complete=1 workload=returned security_acceptance=NOT_ESTABLISHED\n")


def test_driver_valid_complete_run_exits_zero_and_suppresses_unknown_native_text():
    code, lines = _driver().evaluate(0, GOOD, NATIVE)
    assert code == 0
    assert "probe_driver status=complete security_acceptance=NOT_ESTABLISHED" in lines
    assert "probe_native_lines shown=4 dropped=1" in lines
    assert not any("secret" in line or "leaked" in line for line in lines)
    assert "probe_load label=python314 ok=0 error=126" in lines  # a failed LoadLibrary is a result, not a refusal


def test_driver_nonzero_launcher_fails_closed_but_still_reports_native_status():
    code, lines = _driver().evaluate(1, b"", b"CANDIDATE_REFUSAL stage=resume error=0 child=3221225506\nloader_debug_exit code=c0000022\n")
    assert code == 1
    assert "probe_driver status=refused reason=launcher_nonzero security_acceptance=NOT_ESTABLISHED" in lines
    assert "CANDIDATE_REFUSAL stage=resume error=0 child=3221225506" in lines
    assert "loader_debug_exit code=c0000022" in lines


def test_driver_requires_exact_start_and_done_even_when_launcher_exit_is_zero():
    d = _driver()
    code, lines = d.evaluate(0, b"probe_load label=ws2_32 ok=1 error=0\nprobe_done ok=1\n", b"")
    assert code == 1 and "reason=missing_start" in lines[-1]
    code, lines = d.evaluate(0, b"probe_start ok=1\nprobe_load label=ws2_32 ok=1 error=0\n", b"")
    assert code == 1 and "reason=missing_done" in lines[-1]
    code, lines = d.evaluate(0, b"", b"")
    assert code == 1 and "reason=missing_start" in lines[-1]


def test_driver_rejects_free_text_truncation_and_decides_by_exit_code_not_text():
    d = _driver()
    code, lines = d.evaluate(0, GOOD + b"python.exe failed C:\\x\n", b"")
    assert code == 1 and "reason=rejected_line" in lines[-1]
    assert "probe_child_lines count=5 rejected=1" in lines and not any("python.exe" in line for line in lines)
    code, lines = d.evaluate(0, GOOD + b"probe_load label=x ok=1 error=0\n" * 1000, b"")
    assert code == 1 and "reason=truncated" in lines[-1]
    code, _ = d.evaluate(0, GOOD, b"CANDIDATE_CLEANUP complete=0 workload=refused security_acceptance=NOT_ESTABLISHED\n")
    assert code == 0  # native text never flips the exit code; only the launcher exit and child protocol do



def test_duplicate_start_is_not_a_complete_protocol():
    import importlib.util
    spec = importlib.util.spec_from_file_location('bounded_probe_duplicate', D / 'run_probe.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    code, lines = module.evaluate(0, b'probe_start ok=1\nprobe_start ok=1\nprobe_done ok=1\n', b'')
    assert code == 1 and any('missing_start' in line for line in lines)


def test_control_dll_is_trivial_and_workflow_builds_it_hardened_then_pins_it():
    ctl = (D / "probe_ctl.c").read_text()
    assert "return TRUE" in ctl and "#include <windows.h>" in ctl
    for banned in ("LoadLibrary", "CreateFile", "socket", "Reg", "GetProcAddress", "CreateProcess"):
        assert banned not in ctl, banned
    assert "/guard:cf" in WF and "probe_ctl.c" in WF and "probe_ctl.dll" in WF
    import json
    source = json.loads((D / "SOURCE-SHA256.json").read_text())
    assert "verification/windows_integrated_candidate/probe_ctl.c" in source


def test_dumpbin_step_is_read_only_filtered_and_cannot_fail_the_job():
    assert "dumpbin.exe /nologo /dependents" in WF and "dumpbin.exe /nologo /headers" in WF
    assert "catch { Write-Output 'dumpbin_unavailable' }" in WF
    assert "-replace '[^A-Za-z0-9 _.()/-]',''" in WF


def test_probe_lists_every_dumpbin_import_without_changing_the_launcher():
    imports = re.findall(r'try_system\("imp_([A-Za-z0-9_-]+)"', PROBE)
    assert len(imports) == 18 and 'api-ms-win-core-path-l1-1-0' in imports
    assert sum(name.startswith('api-ms-win-crt-') for name in imports) == 12
    assert 'char line[128]' in PROBE
    assert 'HONY_ORDINARY_AC_DIAGNOSTIC' not in (D/'launcher.c').read_text()
    assert 'ORDINARY_AC_DIAGNOSTIC' not in WF



def _norm_cl(src):
    line = next(l for l in WF.splitlines() if '"$root/%s"' % src in l)
    return re.sub(r'probe_(tls_)?ctl\.c|\$probe(Tls|Ctl)', 'X', line)


def test_tls_control_differs_from_plain_control_only_by_tls_and_names():
    tls = (D / "probe_tls_ctl.c").read_text()
    assert "__declspec(thread)" in tls and "return TRUE" in tls
    assert "__declspec(thread)" not in (D / "probe_ctl.c").read_text()
    for banned in ("LoadLibrary", "CreateFile", "socket", "Reg", "GetProcAddress", "CreateProcess"):
        assert banned not in tls, banned
    assert _norm_cl("probe_ctl.c") == _norm_cl("probe_tls_ctl.c")
    labels = re.findall(r'try_(?:system|staged)\("([A-Za-z0-9_-]+)"', PROBE)
    assert labels.index("control_dll") < labels.index("control_tls_dll") < labels.index("python314")
    assert "probe_tls_ctl.dll" in WF


def test_tls_dumpbin_filter_is_anchored_before_prefix_and_reports_count():
    line = next(l for l in WF.splitlines() if "/tls" in l)
    assert "^[0-9A-Fa-f]+ (Start of raw data|End of raw data|Address of index|Address of callbacks|Size of zero fill|Characteristics)$" in line
    assert "dumpbin_tls_fields" in WF and "$f.Count" in WF
    for d in ("python314.dll", "probe_ctl.dll", "probe_tls_ctl.dll"):
        assert d in WF
    import json
    source = json.loads((D / "SOURCE-SHA256.json").read_text())
    assert "verification/windows_integrated_candidate/probe_tls_ctl.c" in source


def test_nocfg_control_reuses_plain_control_source_and_differs_only_in_guard_flag():
    cl = next(l for l in WF.splitlines() if '"/Fe:$probeNoCfg"' in l)
    ref = next(l for l in WF.splitlines() if '"$root/probe_ctl.c"' in l and "$probeCtl" in l)
    assert "/guard:no" in cl and "/guard:cf" not in cl
    assert cl.replace("/guard:no", "/guard:cf").replace("$probeNoCfg", "$probeCtl") == ref
    labels = re.findall(r'try_(?:system|staged)\("([A-Za-z0-9_-]+)"', PROBE)
    assert labels.index("control_tls_dll") < labels.index("control_nocfg_dll") < labels.index("python314")
    assert "probe_nocfg_ctl.dll" in WF


def test_each_probe_compile_checks_its_own_exit_immediately():
    lines = WF.splitlines()
    for i, line in enumerate(lines):
        if "& cl.exe" in line:
            assert "$LASTEXITCODE -ne 0" in lines[i + 1]
