"""Portable Linux tests: classifier, manifest and STATIC properties of the PowerShell files.
RAN on Linux only. No PowerShell exists here, so detect_requirements.ps1 is NEVER executed by these tests;
static text checks and fixtures are not a Windows qualification."""
import json
import re
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import requirements_detect as R  # noqa: E402

MANIFEST = json.loads((HERE / "requirements_manifest.json").read_text())
PS1 = (HERE / "detect_requirements.ps1").read_text()
BAT = (HERE / "check_laptop.bat").read_bytes()
CODE = "\n".join(l for l in PS1.splitlines() if not l.lstrip().startswith("#"))


def py_cand(**kw):
    c = {"path": r"C:\Users\<USER>\AppData\Local\Programs\Python\Python311\python.exe", "source": "registry:3.11", "store_alias": False,
         "trusted_path": True, "exists": True, "static_version": "3.11.9", "static_arch": "64bit", "ran": True,
         "version": "3.11.9", "releaselevel": "final", "bits": 64, "venv": True, "ensurepip": True,
         "executable": r"C:\Users\<USER>\AppData\Local\Programs\Python\Python311\python.exe"}
    c.update(kw)
    return c


def good_facts():
    return {
        "os": {"caption": "Microsoft Windows 10 Pro", "version": "10.0.19045", "arch": "64-bit", "is64bit_os": True},
        "python_candidates": [py_cand()],
        "database": {"collection_ok": True, "errors": [],
                     "services": [{"name": "postgresql-x64-16", "display_name": "postgresql-x64-16", "state": "Running", "start_mode": "Auto",
                                   "exe_name": "pg_ctl.exe", "image_exe": r"C:\Program Files\PostgreSQL\16\bin\pg_ctl.exe",
                                   "file_product_name": "PostgreSQL", "file_product_version": "16.4"}],
                     "programs": [{"display_name": "PostgreSQL 16", "display_version": "16.4", "publisher": "PostgreSQL Global Development Group"}]},
        "wheels": {"folder_exists": True, "sha_file_exists": True, "lock_found": True, "file_count": 44, "sha_line_count": 44,
                   "mismatches": [], "missing": [], "unlisted": [], "not_in_lock": [], "sha_file_hash": "ab" * 32},
        "source": {"main_py_exists": True, "archive_count": 1, "archive_sha256": None},
        "collection_errors": [],
    }


def by(facts=None):
    return {r["item"]: r for r in R.classify(good_facts() if facts is None else facts, MANIFEST)}


# ---------- python ----------
def test_good_python_is_a_finding_not_approval():
    r = by()["python"]
    assert r["status"] == "compatible" and r["compatible"] == "yes (finding only)"
    assert r["approval"] == R.NOT_APPROVED and "not an approval" in r["note"] and "bob" not in json.dumps(r)


@pytest.mark.parametrize("ver", ["3.11.4", "3.11.0", "3.11.18"])
def test_other_311_patch_is_only_a_finding_that_differs_from_report(ver):
    f = good_facts(); f["python_candidates"] = [py_cand(version=ver)]
    r = by(f)["python"]
    assert r["status"] == "compatible" and r["approval"] == R.NOT_APPROVED and "differs from the REPORTED 3.11.9" in r["note"]


def test_approval_only_from_owner_record():
    m = json.loads(json.dumps(MANIFEST)); m["approved_exact_versions"]["python"] = ["3.11.9"]
    r = {x["item"]: x for x in R.classify(good_facts(), m)}["python"]
    assert r["approval"] == "approved by owner record"
    assert MANIFEST["approved_exact_versions"]["python"] == []  # shipped manifest approves nothing


@pytest.mark.parametrize("ver,rl", [("3.11.9rc1", "candidate"), ("3.11.9", "candidate"), ("3.11.9a1", "alpha"), ("3.11", "final"), ("garbage", "final")])
def test_prerelease_or_unparseable_not_compatible(ver, rl):
    f = good_facts(); f["python_candidates"] = [py_cand(version=ver, releaselevel=rl)]
    assert by(f)["python"]["status"] == "incompatible"


@pytest.mark.parametrize("ver,bits", [("3.12.7", 64), ("3.13.1", 64), ("3.14.0", 64), ("3.10.11", 64), ("3.11.9", 32)])
def test_wrong_minor_or_bitness_incompatible(ver, bits):
    f = good_facts(); f["python_candidates"] = [py_cand(version=ver, bits=bits)]
    assert by(f)["python"]["status"] == "incompatible"


def test_312_only_from_registry_not_executed_is_incompatible_not_missing():
    f = good_facts(); f["python_candidates"] = [py_cand(ran=False, version=None, static_version="3.12.7", releaselevel=None, bits=None, venv=None, ensurepip=None, executable=None)]
    r = by(f)["python"]
    assert r["status"] == "incompatible" and "not executed" in r["note"]


def test_311_listed_but_not_run_is_unknown():
    f = good_facts(); f["python_candidates"] = [py_cand(ran=False, version=None, releaselevel=None, bits=None, venv=None, ensurepip=None, executable=None)]
    assert by(f)["python"]["status"] == "unknown"


def test_store_alias_only_is_missing_with_explanation_including_real_store_python():
    f = good_facts(); f["python_candidates"] = [{"path": r"C:\Users\<USER>\AppData\Local\Microsoft\WindowsApps\python.exe", "source": "store-alias",
                                                  "store_alias": True, "trusted_path": False, "exists": True, "ran": False}]
    r = by(f)["python"]
    assert r["status"] == "missing" and "real Microsoft Store Python is excluded" in r["note"]


def test_untrusted_path_never_counts():
    f = good_facts(); f["python_candidates"] = [py_cand(trusted_path=False)]
    r = by(f)["python"]
    assert r["status"] == "unknown" and "not trusted path" in r["note"]


def test_none_found_missing_and_multiple_is_not_a_choice():
    f = good_facts(); f["python_candidates"] = []
    assert by(f)["python"]["status"] == "missing"
    f["python_candidates"] = [py_cand(), py_cand(path="C:\\Program Files\\Python311\\python.exe", executable="C:\\Program Files\\Python311\\python.exe", version="3.11.4")]
    r = by(f)["python"]
    assert "not a choice" in r["note"] and r["approval"] == R.NOT_APPROVED


def test_scalar_instead_of_list_from_powershell():
    f = good_facts(); f["python_candidates"] = py_cand()  # PS 5.1 can emit a lone object
    assert by(f)["python"]["status"] == "compatible"
    f = good_facts(); f["wheels"]["mismatches"] = "x.whl"; f["wheels"]["missing"] = None
    r = by(f)["wheels"]
    assert r["status"] == "incompatible" and "x.whl" in r["note"]
    f = good_facts(); f["database"]["services"] = f["database"]["services"][0]; f["database"]["programs"] = None
    assert by(f)["database"]["status"] == "compatible"


# ---------- database ----------
def test_postgres16_positively_identified_is_finding_only():
    r = by()["database"]
    assert r["status"] == "compatible" and r["approval"] == R.NOT_APPROVED and "never started" in r["note"]


def test_pg_ctl_exact_leaf_name_required():
    f = good_facts(); s = f["database"]["services"][0]
    for bad in ("pg_ctl.exe.bak", "xpostgres.exe", "pg_ctl.exee", "postgres.exe.old"):
        s["exe_name"] = bad; f["database"]["programs"] = []
        assert by(f)["database"]["status"] == "unknown", bad


def test_product_name_must_say_postgresql():
    f = good_facts(); f["database"]["services"][0]["file_product_name"] = "SomethingElse"; f["database"]["programs"] = []
    assert by(f)["database"]["status"] == "unknown"


@pytest.mark.parametrize("name,exe", [("MySQL80", "mysqld.exe"), ("MSSQL$SQLEXPRESS", "sqlservr.exe"), ("MariaDB", "mariadbd.exe"), ("FirebirdServerDefaultInstance", "firebird.exe")])
def test_other_database_stays_unknown_never_missing_or_compatible(name, exe):
    f = good_facts()
    f["database"]["services"] = [{"name": name, "state": "Running", "exe_name": exe, "file_product_name": "x", "file_product_version": "8.0"}]
    f["database"]["programs"] = []
    r = by(f)["database"]
    assert r["status"] == "unknown" and "belongs to this project" in r["note"]


def test_missing_only_when_collection_succeeded_and_nothing_found():
    f = good_facts(); f["database"] = {"collection_ok": True, "errors": [], "services": [], "programs": []}
    m = json.loads(json.dumps(MANIFEST)); m["reported_database_installed"] = False
    assert {x["item"]: x for x in R.classify(f, m)}["database"]["status"] == "missing"
    assert by(f)["database"]["status"] == "unknown"  # shipped manifest: owner reports one installed
    f["database"]["collection_ok"] = False; f["database"]["errors"] = ["service list failed"]
    assert by(f)["database"]["status"] == "unknown"
    f["database"] = None
    assert by(f)["database"]["status"] == "unknown"


def test_postgres_other_major_and_mixed():
    f = good_facts(); f["database"]["services"][0]["file_product_version"] = "15.8"; f["database"]["programs"] = [{"display_name": "PostgreSQL 15", "display_version": "15.8"}]
    assert by(f)["database"]["status"] == "incompatible"
    f["database"]["programs"].append({"display_name": "PostgreSQL 16", "display_version": "16.4"})
    assert by(f)["database"]["status"] == "unknown"  # mixed majors: not a choice


def test_plan_never_proposes_second_db_or_service_start_when_a_db_exists():
    f = good_facts(); f["database"]["services"] = [{"name": "MySQL80", "exe_name": "mysqld.exe"}]; f["database"]["programs"] = []
    lines = "\n".join(R.plan(R.classify(f, MANIFEST), MANIFEST))
    assert "not permission to use it, to download a second one, or to start a service" in lines
    assert "Candidate:" not in lines.split("database:")[1].split("\n")[0]


# ---------- windows / wheels / source ----------
def test_windows_rules():
    f = good_facts(); f["os"]["is64bit_os"] = False
    assert by(f)["windows"]["status"] == "incompatible"
    f = good_facts(); f["os"]["is64bit_os"] = None; f["os"]["arch"] = "weird"
    assert by(f)["windows"]["status"] == "unknown"
    f = good_facts(); f["os"] = None
    assert by(f)["windows"]["status"] == "unknown"
    f = good_facts(); f["os"]["version"] = "6.1.7601"
    assert by(f)["windows"]["status"] == "incompatible"


def test_wheels_rules():
    base = good_facts
    for key, val in (("mismatches", ["a.whl"]), ("missing", ["b.whl"]), ("unlisted", ["c.whl"]), ("not_in_lock", ["d.whl"]),
                     ("sha_line_count", 43), ("file_count", 45)):
        f = base(); f["wheels"][key] = val
        assert by(f)["wheels"]["status"] == "incompatible", key
    f = base(); f["wheels"]["lock_found"] = False
    assert by(f)["wheels"]["status"] == "unknown"
    f = base(); f["wheels"]["folder_exists"] = False
    assert by(f)["wheels"]["status"] == "missing"
    assert by()["wheels"]["status"] == "compatible"


def test_source_is_never_compatible_and_never_called_verified():
    r = by()["source"]
    assert r["status"] == "unknown" and "NOT verified" in r["note"]
    f = good_facts(); f["source"]["archive_sha256"] = "4e02e79f8c4c81a572800a5cc9004d9dedb24468a84f54116324fd3cd8d33ac2"
    r = by(f)["source"]
    assert r["status"] == "unknown" and "tree is unverified" in r["note"]
    f["source"]["archive_sha256"] = "0" * 64
    assert by(f)["source"]["status"] == "incompatible"
    f["source"]["main_py_exists"] = False
    assert by(f)["source"]["status"] == "missing"


def test_uncollected_facts_all_unknown():
    assert all(x["status"] == "unknown" for x in R.classify({}, MANIFEST))


# ---------- facts reader ----------
@pytest.mark.parametrize("enc,bom", [("utf-8", b""), ("utf-8", b"\xef\xbb\xbf"), ("utf-16-le", b"\xff\xfe"), ("utf-16-be", b"\xfe\xff")])
def test_reader_accepts_bom_encodings(tmp_path, enc, bom):
    p = tmp_path / "f.json"; p.write_bytes(bom + json.dumps(good_facts()).encode(enc))
    assert R.load_facts(str(p))["os"]["caption"].startswith("Microsoft")


@pytest.mark.parametrize("blob", [b"", b"\xff\xfe\x00", b"not json", b"[1,2]", b"\xff\xff\xff"])
def test_reader_invalid_input_is_a_clear_error(tmp_path, blob, capsys):
    p = tmp_path / "f.json"; p.write_bytes(blob)
    assert R.main(["--facts", str(p)]) == 2 and "ERROR" in capsys.readouterr().out


def test_cli_prints_table_summary_plan_redacted(tmp_path, capsys):
    f = good_facts(); f["python_candidates"][0]["path"] = r"C:\Users\bob\x\python.exe"; f["python_candidates"][0]["executable"] = r"C:\Users\bob\x\python.exe"
    p = tmp_path / "f.json"; p.write_text(json.dumps(f))
    assert R.main(["--facts", str(p), "--plan"]) == 1
    out = capsys.readouterr().out
    assert "COPYABLE SUMMARY" in out and "PLAN" in out and "bob" not in out and "C:\\Users\\<USER>" in out


# ---------- manifest ----------
def test_manifest_hash_honesty():
    py = MANIFEST["items"]["python"]["download"]
    assert py["sha256"] is None and py["hash_status"] == "BLOCKED_NO_OFFICIAL_SHA256" and py["blocker"]
    assert re.fullmatch(r"[0-9a-f]{32}", py["md5_published_by_python_org"])
    pg = MANIFEST["items"]["database"]["download"]
    assert pg["hash_status"].startswith("OBSERVED") and "NOT_INDEPENDENTLY" in pg["hash_status"] and pg["official_file_url"] is None
    assert re.fullmatch(r"[0-9a-f]{64}", pg["sha256"])
    assert MANIFEST["reported_laptop_python"] == "3.11.9" and "postgresql" not in MANIFEST["items"]


def test_source_hash_pinned():
    assert MANIFEST["items"]["source"]["rule"]["archive_sha256"].startswith("4e02e79f")


# ---------- static checks (text only) ----------
MUTATORS = re.compile(r"\b(Install-|Uninstall-|Set-(?!Location)|New-(?!Object)|Remove-|Copy-Item|Move-Item|Rename-Item|Out-File|Add-Content|"
                      r"Set-Content|Start-Process|Start-Service|Stop-Service|Restart-|Invoke-WebRequest|Invoke-RestMethod|Invoke-Expression|iex\b|"
                      r"Start-BitsTransfer|iwr\b|curl|wget|winget|choco|msiexec|reg\s+add|reg\s+delete|icacls|takeown|netsh|Set-Acl|"
                      r"Enable-|Disable-|Register-|Unregister-|Expand-Archive|\[IO\.File\]::Write|Tee-Object|Export-|ConvertTo-Csv|>>|\s>\s)", re.I)


def test_ps1_has_no_mutating_network_or_file_writing_commands():
    m = MUTATORS.search(CODE)
    assert m is None, m.group(0)


def test_ps1_executes_only_python_candidates_and_the_classifier():
    calls = re.findall(r"(?m)(?:^|[=|;{]\s*)&\s+\$[A-Za-z_.:]+", CODE)
    assert len(calls) == 2, calls  # candidate probe, classifier run
    assert "--version" not in CODE and "where.exe" not in CODE and "Get-NetTCPConnection" not in CODE and "citool" not in CODE
    assert "Start-Process" not in CODE


def test_ps1_never_touches_data_dirs_secrets_or_ports():
    low = CODE.lower()
    assert ".env" not in low and "password" not in low and "database_url" not in low
    assert "\\data" not in low and "pgpass" not in low and "psql" not in low and "invoke-sqlcmd" not in low


def test_ps1_service_command_line_is_never_emitted():
    uses = [l for l in CODE.splitlines() if "PathName" in l]
    assert len(uses) == 1 and "-match" in uses[0]  # only parsed for the exe path, never output


def test_ps1_exe_path_regex_extracts_exe_only_and_drops_arguments():
    rx = re.search(r"-match '(\^\\s\*\"\?\(\.\+\?\\\.exe\)\"\?\(\\s\|\$\))'", PS1)
    assert rx, "regex not found in ps1"
    pat = re.compile(rx.group(1))
    for path_name, exe in [('"C:\\Program Files\\PostgreSQL\\16\\bin\\pg_ctl.exe" runservice -N "postgresql-x64-16" -D "C:\\Program Files\\PostgreSQL\\16\\data" -w', "C:\\Program Files\\PostgreSQL\\16\\bin\\pg_ctl.exe"),
                           ('C:\\Windows\\System32\\x.exe -p secret', "C:\\Windows\\System32\\x.exe"),
                           ('"C:\\Program Files\\MySQL\\bin\\mysqld.exe" --defaults-file="C:\\ProgramData\\my.ini" MySQL80', "C:\\Program Files\\MySQL\\bin\\mysqld.exe")]:
        m = pat.match(path_name)
        assert m and m.group(1) == exe and "secret" not in m.group(1)


def test_ps1_redacts_user_pc_names_and_uses_file_count_key():
    assert "USERNAME" in PS1 and "COMPUTERNAME" in PS1 and "<USER>" in PS1
    assert "file_count" in PS1 and not re.search(r"\bcount\s*=\s*0", CODE) and "$w.count" not in CODE
    for l in CODE.splitlines():
        if "image_exe" in l:
            assert "Redact" in l


def test_ps1_trusted_python_rules_present():
    assert "Test-TrustedPython" in CODE and '-ne "python.exe"' in CODE and "WindowsApps" in CODE
    assert re.search(r"static_version.*3\\\.11|\$sv -match '\^3\\\.11", CODE)  # only statically-3.11 candidates are executed
    assert "-I -B" in CODE
    assert "Set-Location -LiteralPath $env:SystemRoot" in CODE


def test_ps1_writes_no_file_and_says_so():
    assert "writes NO file" in PS1 and "Out-File" not in PS1


def test_bat_wrapper_properties():
    assert b"\r\n" in BAT and b"\n" not in BAT.replace(b"\r\n", b"")
    t = BAT.decode()
    assert '-NoProfile -ExecutionPolicy Bypass -File "%~dp0detect_requirements.ps1"' in t
    assert "-Command" not in t and "-Scope" not in t and "Set-ExecutionPolicy" not in t and ">" not in t.replace("rem", "").split("powershell.exe")[-1]
    assert "pause" in t and "process" in t.lower()
    assert "System32\\WindowsPowerShell\\v1.0\\powershell.exe" in t  # full path, no PATH search



def test_postgres16_beside_unidentified_db_is_unknown():
    f = good_facts()
    f["database"]["services"].append({"name": "MySQL80", "exe_name": "mysqld.exe", "state": "Running"})
    r = by(f)["database"]
    assert r["status"] == "unknown" and "NOT identified" in r["note"] and "MySQL80" in r["note"]
    f = good_facts(); f["database"]["programs"].append({"display_name": "SQL Server 2019", "display_version": "15.0"})
    assert by(f)["database"]["status"] == "unknown"


def test_program_entry_without_service_says_service_not_seen():
    f = good_facts(); f["database"]["services"] = []
    r = by(f)["database"]
    assert r["status"] == "unknown" and "service was not seen" in r["note"]


def test_driver_entries_not_counted_but_mentioned():
    f = good_facts(); f["database"]["driver_entries"] = ["Microsoft ODBC Driver 17 for SQL Server"]
    r = by(f)["database"]
    assert r["status"] == "compatible" and "listed separately" in r["note"]
    f["database"] = {"collection_ok": True, "errors": [], "services": [], "programs": [], "driver_entries": ["x driver"]}
    m = json.loads(json.dumps(MANIFEST)); m["reported_database_installed"] = False
    r = {x["item"]: x for x in R.classify(f, m)}["database"]
    assert r["status"] == "missing" and "name patterns only" in r["note"]
    assert by(f)["database"]["status"] == "unknown" and "owner reports" in by(f)["database"]["note"]


def test_errs_kept_when_notes_exist():
    f = good_facts(); f["python_candidates"] = [py_cand(trusted_path=False)]; f["collection_errors"] = ["registry read failed"]
    assert "registry read failed" in by(f)["python"]["note"]


def test_ps1_runner_only_from_probed_candidates_and_checks_exit():
    assert "$script:runnerPath" in CODE and "UNHASHED" in PS1
    assert "foreach ($c in $found.Values) {\n  if ((Test-TrustedPython" not in CODE


def test_ps1_driver_split_and_narrow_pattern():
    assert "$driverPattern" in CODE and "Publisher)\") -match" not in CODE
    pat = re.search(r"\$dbPattern = '([^']+)'", PS1).group(1)
    assert "|oracle|" not in pat and "sqlite" not in pat
    assert "oracle (database|xe)" in pat


def test_ps1_redact_is_segment_based():
    assert "(?<=[\\\\/])" in PS1 and "-replace [regex]::Escape($n)" not in PS1


def test_bat_does_not_claim_success_blindly():
    t = BAT.decode()
    assert "Nothing was changed or saved" not in t.replace("If you saw an error above", "") or "ERRORLEVEL" in t
    assert "If you saw an error" in t


def test_one_install_seen_twice_wording_and_shipped_flag():
    r = by()["database"]
    assert "probably one install seen twice" in r["note"] and "several entries" not in r["note"]
    assert MANIFEST["reported_database_installed"] is True


def test_summary_caveat_and_ps1_success_by_summary_text():
    assert "NOT redacted" in "\n".join(R.summary(R.classify(good_facts(), MANIFEST)))
    assert "COPYABLE SUMMARY" in PS1 and "-eq 0 -or $LASTEXITCODE -eq 1" not in PS1
    assert "NOT redacted" in BAT.decode()


def _ps_patterns():
    db = re.search(r"\$dbPattern = '([^']+)'", PS1).group(1).replace("(?i)", "")
    drv = re.search(r"\$driverPattern = '([^']+)'", PS1).group(1).replace("(?i)", "")
    return re.compile(db, re.I), re.compile(drv, re.I)


@pytest.mark.parametrize("name", ["Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.38.33135", "Microsoft Visual C++ 2015-2022 Redistributable (x86) - 14.38.33135",
                                  "Microsoft Edge", "Microsoft Edge WebView2 Runtime", "Python 3.11.9 (64-bit)", "Python Launcher", "Java 8 Update 301",
                                  "Oracle VM VirtualBox 7.0.12", "Microsoft Update Health Tools", "Windows SDK AddOn", "Ingress Tools", "Mariana Viewer"])
def test_ordinary_programs_do_not_match_db_pattern(name):
    db, _ = _ps_patterns()
    assert not db.search(name), name


@pytest.mark.parametrize("name", ["PostgreSQL 16", "MySQL Server 8.0", "MariaDB 10.11 (x64)", "Microsoft SQL Server 2019 (64-bit)", "Redis on Windows 5.0", "IBM DB2 Client", "Firebird 4.0", "MongoDB 7.0.2"])
def test_real_database_names_match(name):
    db, _ = _ps_patterns()
    assert db.search(name), name


@pytest.mark.parametrize("name", ["Microsoft OLE DB Driver 18 for SQL Server", "Microsoft ODBC Driver 17 for SQL Server", "MySQL Connector/NET", "PostgreSQL ODBC Driver", "SQL Server Management Studio"])
def test_drivers_and_tools_are_split_out(name):
    db, drv = _ps_patterns()
    assert db.search(name) and drv.search(name), name


def test_postgres_entry_with_drivers_and_vc_redist_stays_compatible():
    db, drv = _ps_patterns()
    names = ["Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.38.33135", "Microsoft Edge", "Python 3.11.9 (64-bit)",
             "Microsoft OLE DB Driver 18 for SQL Server", "PostgreSQL 16"]
    progs = [{"display_name": n, "display_version": "16.4"} for n in names if db.search(n) and not drv.search(n)]
    drivers = [n for n in names if db.search(n) and drv.search(n)]
    assert [p["display_name"] for p in progs] == ["PostgreSQL 16"]
    f = good_facts(); f["database"]["programs"] = progs; f["database"]["driver_entries"] = drivers
    assert by(f)["database"]["status"] == "compatible"


def test_no_smartscreen_or_applocker_workaround_text():
    for f in HERE.glob("*"):
        if f.suffix in (".md", ".ps1", ".bat") and f.name != "test_requirements_detect.py":
            low = f.read_text().lower()
            assert "run anyway" not in low and "unblock-file" not in low and "more info" not in low, f.name
    assert "reports the exact message and stops" in (HERE / "README_BOOTSTRAP.md").read_text()
