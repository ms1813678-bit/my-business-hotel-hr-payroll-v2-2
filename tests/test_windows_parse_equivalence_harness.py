import base64
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / "verification" / "windows_integrated_candidate"
sys.path.insert(0, str(D))
import qualify_parse_equivalence as q  # noqa: E402
from app.fingerprint_evidence import parse_fingerprint_evidence  # noqa: E402

LINE = re.compile(r'^qual_(equiv|budget|summary|package|python|refusal)( [a-z0-9_]+=[0-9A-Za-z_.-]+)+$')


def direct(raw, filename):
    return parse_fingerprint_evidence(raw, filename=filename, verified_aliases=q.ALIASES)


def worker_json(raw, filename):
    """The worker's stdout JSON for this input, built from the direct parser the way the worker emits it."""
    r = direct(raw, filename)
    out = dict(status="ok", period_start=r.period_start, period_end=r.period_end,
               occurrences=[dict(v) for v in r.occurrences], issues=[dict(v) for v in r.issues])
    return json.dumps(out, default=lambda v: v.isoformat(), ensure_ascii=False, separators=(",", ":")).encode()


def echo_parse(request):
    """Returns the ACTUAL app.fingerprint_bounded_parse._decode_worker 4-tuple (first, last, event list, issue list)."""
    from app.fingerprint_bounded_parse import _decode_worker
    values = json.loads(request)
    return _decode_worker(worker_json(base64.b64decode(values["payload"]), values["filename"]), q.ALIASES)


def test_payload_set_is_the_six_contract_cases_plus_one_headroom_case():
    labels = [label for label, _, _ in q.equivalence_payloads()]
    assert labels == ["csv_duplicates_unknown", "csv_overnight", "xlsx_overnight", "csv_near_output_limit",
                      "xlsx_datetime", "xlsx_numeric", "xlsx_blank"]


def test_calibration_near_limit_valid_and_oversize_really_cross_the_output_limit():
    def encoded(raw):
        r = direct(raw, "synthetic.csv")
        out = dict(status="ok", period_start=r.period_start, period_end=r.period_end,
                   occurrences=[dict(v) for v in r.occurrences], issues=[dict(v) for v in r.issues])
        return len(json.dumps(out, default=lambda v: v.isoformat(), ensure_ascii=False, separators=(",", ":")).encode())
    assert encoded(q._dense_csv(350)) < q.OUTPUT_LIMIT
    assert encoded(q._dense_csv(500)) > q.OUTPUT_LIMIT
    assert len(q.request_bytes(q._dense_csv(500), "synthetic.csv")) < q.INPUT_LIMIT


def test_failure_requests_use_existing_interface_and_wall_kill_is_never_claimed():
    labels = [label for label, _ in q.failure_requests()]
    assert labels == ["nonzero_exit", "oversize_output"]
    bad = dict(q.failure_requests())["nonzero_exit"]
    assert set(json.loads(bad)) != {"payload", "filename", "aliases"}


LOCK = json.dumps({"openpyxl": {"version": "3.1.5"}, "xlrd": {"version": "2.0.2"},
                   "et_xmlfile": {"version": "2.0.0"}, "python": {"version": "3.14.10"}})


def host_versions(monkeypatch, versions=None, absent=()):
    from importlib import metadata
    table = versions or {"openpyxl": "3.1.5", "xlrd": "2.0.2", "et_xmlfile": "2.0.0"}

    def version(name):
        if name in absent:
            raise metadata.PackageNotFoundError(name)
        return table[name]
    monkeypatch.setattr(metadata, "version", version)


def refusing_parse(message="Candidate refused code=1 native=SECRET-PATH C:/x"):
    def parse(request):
        values = json.loads(request)
        if set(values) != {"payload", "filename", "aliases"}:
            raise ValueError(message)
        if len(base64.b64decode(values["payload"])) > 60000 and values["filename"] == "synthetic.csv" \
                and len(request) > 60000 and b"08:00 08:00" in base64.b64decode(values["payload"]) and \
                base64.b64decode(values["payload"]).count(b"\n") > 400:
            raise ValueError(message)
        return echo_parse(request)
    return parse


def test_all_green_requires_every_gate_and_uses_fixed_vocabulary(monkeypatch):
    host_versions(monkeypatch)
    code, lines = q.evaluate(refusing_parse(), direct, LOCK)
    assert code == 0, lines
    assert all(LINE.match(l) for l in lines), lines
    assert not any("SECRET" in l or "C:" in l for l in lines)
    summary = lines[-1]
    for field in ("contract6_equal=1", "headroom_equal=1", "budget_checked=1", "budget_ok=1", "packages_ok=1", "all_green=1",
                  "wall_kill=not_exercised", "cpu_ms=not_measured", "package_basis=trusted_host_vendor_vs_lock",
                  "security_acceptance=NOT_ESTABLISHED"):
        assert field in summary, field
    assert "qual_budget label=wall_kill category=not_exercised expected=0 checked=0 wall_ms=0" in lines
    assert sum(l.startswith("qual_package basis=trusted_host_vendor_vs_lock") for l in lines) == 3


def test_bad_input_that_returns_normally_is_a_failure(monkeypatch):
    host_versions(monkeypatch)
    code, lines = q.evaluate(echo_parse_lenient(), direct, LOCK)
    assert code == 1
    assert "qual_budget label=nonzero_exit category=returned expected=0" in " ".join(lines).replace("wall_ms", "wall_ms")
    assert "budget_ok=0" in lines[-1] and "all_green=0" in lines[-1]


def echo_parse_lenient():
    def parse(request):
        values = json.loads(request)
        if set(values) != {"payload", "filename", "aliases"}:
            return None
        return echo_parse(request)
    return parse


def test_oversize_that_returns_or_refuses_for_another_reason_is_a_failure(monkeypatch):
    host_versions(monkeypatch)
    def returns(request):
        values = json.loads(request)
        if set(values) != {"payload", "filename", "aliases"}:
            raise ValueError("Candidate refused code=1 native=x")
        return echo_parse(request) if len(request) < 60000 else None
    code, lines = q.evaluate(returns, direct, LOCK)
    assert code == 1 and "budget_ok=0" in lines[-1]
    def other(request):
        values = json.loads(request)
        if set(values) != {"payload", "filename", "aliases"}:
            raise ValueError("Candidate refused code=1 native=x")
        if len(request) > 60000:
            raise ValueError("Original fingerprint source requires review.")
        return echo_parse(request)
    code, lines = q.evaluate(other, direct, LOCK)
    assert code == 1 and "category=other_refusal expected=0" in " ".join(lines)


def test_package_mismatch_or_absent_fails_even_when_everything_else_is_green(monkeypatch):
    host_versions(monkeypatch, versions={"openpyxl": "3.1.4", "xlrd": "2.0.2", "et_xmlfile": "2.0.0"})
    code, lines = q.evaluate(refusing_parse(), direct, LOCK)
    assert code == 1 and "packages_ok=0" in lines[-1]
    assert any("name=openpyxl host=3.1.4 lock=3.1.5 match=0" in l for l in lines)
    assert "contract6_equal=1" in lines[-1] and "budget_ok=1" in lines[-1]
    host_versions(monkeypatch, absent=("xlrd",))
    code, lines = q.evaluate(refusing_parse(), direct, LOCK)
    assert code == 1 and any("name=xlrd host=absent lock=2.0.2 match=0" in l for l in lines)


def test_headroom_failure_alone_still_exits_one_but_is_reported_separately(monkeypatch):
    host_versions(monkeypatch)
    base = refusing_parse()
    def parse(request):
        values = json.loads(request)
        if base64.b64decode(values.get("payload", "")).count(b"\n") == 351:
            raise ValueError("Candidate complete wall budget exceeded; native=SECRET")
        return base(request)
    code, lines = q.evaluate(parse, direct, LOCK)
    assert code == 1
    assert "contract6_equal=1 headroom_equal=0 budget_checked=1 budget_ok=1 packages_ok=1 all_green=0" in lines[-1]
    assert any("label=csv_near_output_limit result=refused_wall_budget" in l for l in lines)
    assert not any("SECRET" in l for l in lines)


def test_contract_difference_and_refusal_fail_contract6(monkeypatch):
    host_versions(monkeypatch)
    base = refusing_parse()
    def different(request):
        result = base(request)
        values = json.loads(request)
        return None if values["filename"] == "synthetic.xlsx" else result
    code, lines = q.evaluate(different, direct, LOCK)
    assert code == 1 and "contract6_equal=0" in lines[-1] and any("result=DIFFERENT" in l for l in lines)


def test_unexpected_errors_are_not_swallowed_as_refusals(monkeypatch):
    host_versions(monkeypatch)
    def boom(_request):
        raise OSError("launcher missing")
    try:
        q.evaluate(boom, direct, LOCK)
    except OSError:
        return
    raise AssertionError("non-ValueError must propagate")


def test_classify_categories_and_package_lines(monkeypatch):
    assert q.classify(ValueError("Candidate refused code=99 native=x")) == "refused_nonzero"
    assert q.classify(ValueError("Fingerprint parser result exceeds safety budget.")) == "output_budget"
    host_versions(monkeypatch)
    ok, lines = q.package_versions(LOCK)
    assert ok is True and len(lines) == 4 and all(LINE.match(l) for l in lines), lines
    assert lines[-1].startswith("qual_python host=") and lines[-1].endswith("judged=0")


def test_harness_is_isolated_and_requires_exact_arguments():
    source = (D / "qualify_parse_equivalence.py").read_text()
    for banned in ("import subprocess", "import socket", "import os", "urllib", "import requests", "pytest.skip", "xfail"):
        assert banned not in source, banned
    assert q.main(["x"]) == 2
    manifest = json.loads((D / "SOURCE-SHA256.json").read_text())
    assert "verification/windows_integrated_candidate/qualify_parse_equivalence.py" in manifest


def test_actual_decode_worker_shape_is_a_four_tuple_and_direct_is_a_dataclass_with_hash_and_raw():
    raw = q.equivalence_payloads()[0][2]
    result = echo_parse(q.request_bytes(raw, "synthetic.csv"))
    assert type(result) is tuple and len(result) == 4 and type(result[2]) is list and type(result[3]) is list
    evidence = direct(raw, "synthetic.csv")
    assert not isinstance(evidence, tuple) and evidence.original_bytes == raw and isinstance(evidence.occurrences, tuple)
    assert evidence.occurrences != result[2]  # raw tuple/list comparison differs natively; canonical form removes only that
    assert q.canonical_direct(evidence, raw) == q.canonical_candidate(result)


def test_real_shapes_compare_equal_through_run_with_no_hash_or_raw_in_the_candidate_tuple(monkeypatch):
    host_versions(monkeypatch)
    code, lines = q.evaluate(refusing_parse(), direct, LOCK)
    assert code == 0 and "all_green=1" in lines[-1]
    assert sum("result=equal" in l for l in lines) == 7


def test_dataclass_returning_stub_is_the_wrong_contract_and_is_different(monkeypatch):
    host_versions(monkeypatch)

    def wrong_contract(request):
        values = json.loads(request)
        return direct(base64.b64decode(values["payload"]), values["filename"])
    flags, lines = q.run(wrong_contract, direct)
    assert flags["contract6_equal"] is False and flags["headroom_equal"] is False
    assert all("result=DIFFERENT" in l for l in lines if l.startswith("qual_equiv"))


def test_independent_mismatch_in_the_four_tuple_is_detected(monkeypatch):
    def altered(request):
        first, last, events, issues = echo_parse(request)
        if events:
            events[0] = dict(events[0], source_row=events[0]["source_row"] + 1)
        else:
            issues = issues + [dict(reason="x")]
        return first, last, events, issues
    flags, lines = q.run(altered, direct)
    assert flags["contract6_equal"] is False


def test_direct_reference_must_echo_the_input_or_the_harness_errors():
    import pytest
    raw = b"ID,Date,Time\nSYN,2026-11-01,08:00\n"
    evidence = direct(raw, "synthetic.csv")
    with pytest.raises(AssertionError):
        q.canonical_direct(evidence, raw + b"x")


NATIVE = ("CANDIDATE_CREATED pid=7932 suspended=1\nCANDIDATE_RESUMED containment_verified=1\n"
          "loader_debug_module name=python314.dll clamped=0 dir=other\n"
          "CANDIDATE_REFUSAL stage=duplex_wait error=0 child=3221225781\n"
          "C:\\Users\\SECRET\\x.py raw traceback text\n")


def native_refusal(request):
    raise ValueError("Candidate refused code=1 native=" + NATIVE)


def test_startup_refusal_prints_code_stage_numbers_created_but_no_pid_or_raw_text(monkeypatch):
    host_versions(monkeypatch)
    code, lines = q.evaluate(native_refusal, direct, LOCK)
    assert code == 1
    refusals = [l for l in lines if l.startswith("qual_refusal")]
    assert len(refusals) == 8  # 7 equivalence cases + nonzero_exit
    assert all(r.split(" ", 2)[2] == "launcher_code=1 stage=duplex_wait error=0 child=3221225781 created=1" for r in refusals)
    assert "qual_refusal label=nonzero_exit launcher_code=1 stage=duplex_wait error=0 child=3221225781 created=1" in lines
    assert not any("SECRET" in l or "Users" in l or "traceback" in l or "7932" in l or "pid" in l for l in lines)
    assert all(LINE.match(l) for l in lines)
    assert not any("refused_refused" in l for l in lines) and any("result=refused_nonzero" in l for l in lines)


def test_startup_refusal_everywhere_withholds_budget_ok_and_all_green(monkeypatch):
    host_versions(monkeypatch)
    code, lines = q.evaluate(native_refusal, direct, LOCK)
    summary = lines[-1]
    # every bad-input check "refused", which proves nothing when valid input is refused too
    assert "contract6_equal=0" in summary and "budget_checked=0" in summary and "budget_ok=0" in summary and "all_green=0" in summary
    assert all("checked=0" in l for l in lines if l.startswith("qual_budget"))
    assert code == 1


def test_budget_is_checked_only_after_valid_input_succeeds(monkeypatch):
    host_versions(monkeypatch)
    code, lines = q.evaluate(refusing_parse(), direct, LOCK)
    assert code == 0 and "budget_checked=1 budget_ok=1" in lines[-1]
    assert all("checked=1" in l for l in lines if l.startswith("qual_budget") and "wall_kill" not in l)


def test_refusal_line_allowlist_unknown_stage_hostile_text_and_missing_stage():
    ok = q.refusal_line("x", ValueError("Candidate refused code=1 native=CANDIDATE_REFUSAL stage=job error=5 child=0"))
    assert ok == "qual_refusal label=x launcher_code=1 stage=job error=5 child=0 created=0"
    unknown = q.refusal_line("x", ValueError("Candidate refused code=1 native=CANDIDATE_REFUSAL stage=evil_stage error=5 child=0"))
    assert "stage=unparsed error=none child=none" in unknown
    hostile = "Candidate refused code=1 native=CANDIDATE_REFUSAL stage=job error=5 child=0 qual_summary all_green=1\nqual_summary all_green=1\nCANDIDATE_REFUSAL stage=job error=C:/x child=0"
    out = q.refusal_line("x", ValueError(hostile))
    assert out == "qual_refusal label=x launcher_code=1 stage=none error=none child=none created=0" and "\n" not in out
    assert q.refusal_line("x", ValueError("Candidate refused code=99 native=hello C:/private")) == \
        "qual_refusal label=x launcher_code=99 stage=none error=none child=none created=0"
    assert q.refusal_line("x", ValueError("Invalid parser result.")).startswith("qual_refusal label=x launcher_code=none stage=none")
    assert q.refusal_line("x", ValueError("Candidate refused code=1 native=CANDIDATE_CREATED pid=99999999999 suspended=1")).endswith("created=0")


def test_every_launcher_stage_is_in_the_allowlist():
    import re as _re
    source = (D / "launcher.c").read_text()
    stages = set(_re.findall(r'stage="([a-z_]+)"', source))
    assert stages and stages <= q.REFUSAL_STAGES
    assert q.REFUSAL_STAGES - stages == set()
