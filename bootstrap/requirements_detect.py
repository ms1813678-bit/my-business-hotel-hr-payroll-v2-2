"""Classifier and reporter for the Option B requirements check. PROTOTYPE, read-only, stdlib only.

Normally run by detect_requirements.ps1 (single entry point), which pipes the collected facts in.
Input: facts JSON (stdin with --facts - , or a file) + requirements_manifest.json.
Output: one line per item: required / found / version / path / compatible / approval / status / note.
It never downloads, installs, replaces or modifies anything and never reads secrets.

Status: compatible | incompatible | missing | unknown.
  compatible = meets the written rule, as a FINDING only. It is never approval.
  approval   = "not approved (owner decision)" unless the exact version is listed in the manifest's
               approved_exact_versions (empty today), so finding 3.11.4 or 3.11.9 is not approval.
  incompatible -> never valid, never replaced. unknown -> never valid.
"""
import argparse
import json
import re
import sys
from pathlib import Path

OK, BAD, MISSING, UNKNOWN = "compatible", "incompatible", "missing", "unknown"
NOT_APPROVED = "not approved (owner decision)"


def redact_path(p):
    if not p:
        return p
    return re.sub(r"(?i)^([a-z]:\\users\\)[^\\]+", r"\1<USER>", p)


def as_list(v):
    """The PowerShell side can emit a lone string where a list is meant; coerce."""
    if v is None:
        return []
    if isinstance(v, (str, dict)):
        return [v] if v else []
    return list(v)


def item(name, required, found, version=None, path=None, status=UNKNOWN, note="", approval="-"):
    compat = {OK: "yes (finding only)", BAD: "no", MISSING: "n/a", UNKNOWN: "unknown"}[status]
    return {"item": name, "required": required, "found": "yes" if found else ("no" if found is False else "unknown"),
            "version": version, "path": redact_path(path), "compatible": compat,
            "approval": approval if status == OK else "-", "status": status, "note": note}


def approval_for(manifest, key, version):
    ok = as_list(manifest.get("approved_exact_versions", {}).get(key))
    return "approved by owner record" if version and version in ok else NOT_APPROVED


def check_windows(facts, manifest):
    rule, required = manifest["items"]["windows"]["rule"], manifest["items"]["windows"]["required"]
    w = facts.get("os")
    if not w or not w.get("version"):
        return item("windows", required, None, status=UNKNOWN, note="OS facts not collected")
    m = re.match(r"(\d+)\.\d+\.(\d+)", w["version"])
    major_ok = bool(m) and int(m.group(1)) == rule["major"]
    is64 = w.get("is64bit_os")
    if is64 is None:
        a = str(w.get("arch", "")).lower()
        is64 = True if a.startswith("64") else (False if a.startswith("32") else None)
    note = "caption=%s; build=%s" % (w.get("caption"), m.group(2) if m else "?")
    if is64 is None:
        return item("windows", required, True, w["version"], None, UNKNOWN, note + "; bitness could not be determined")
    return item("windows", required, True, w["version"], None, OK if major_ok and is64 else BAD,
                note + "; 64-bit=%s" % is64, NOT_APPROVED)


FINAL_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
STATIC_VERSION = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?$")


def minor_of(version):
    m = STATIC_VERSION.match(version or "")
    return "%s.%s" % (m.group(1), m.group(2)) if m else None


def check_python(facts, manifest):
    rule, required = manifest["items"]["python"]["rule"], manifest["items"]["python"]["required"]
    reported = manifest["reported_laptop_python"]
    cands = facts.get("python_candidates")
    if cands is None:
        return item("python", required, None, status=UNKNOWN, note="Python facts not collected")
    cands = [cands] if isinstance(cands, dict) else as_list(cands)
    if facts.get("collection_errors"):
        errs = "; collection problems: " + ", ".join(as_list(facts["collection_errors"]))
    else:
        errs = ""
    real = [c for c in cands if not c.get("store_alias")]
    if not cands:
        return item("python", required, False, status=MISSING, note="no registry entry or fixed-folder python.exe found" + errs)
    if not real:
        return item("python", required, False, status=MISSING,
                    note="only the Microsoft Store alias was seen. It is never run, and a real Microsoft Store Python is excluded the same way, so it reads as missing" + errs)
    notes, scored = [], []
    for c in real:
        if not c.get("trusted_path"):
            notes.append("not trusted path, not run: %s" % redact_path(c.get("path")))
            continue
        if c.get("ran"):
            v = c.get("version") or ""
            m = FINAL_VERSION.match(v)
            if not m or c.get("releaselevel", "final") != "final":
                scored.append((BAD, c, "not a final release (%s %s)" % (v, c.get("releaselevel"))))
                continue
            mn = "%s.%s" % m.group(1, 2)
            ok = mn == rule["minor"] and c.get("bits") == rule["bits"] and all(c.get(k) for k in rule["needs"])
            why = []
            if mn != rule["minor"]:
                why.append("minor %s is not %s" % (mn, rule["minor"]))
            if c.get("bits") != rule["bits"]:
                why.append("%s-bit" % c.get("bits"))
            why += ["%s module missing" % k for k in rule["needs"] if not c.get(k)]
            scored.append((OK if ok else BAD, c, "; ".join(why)))
        else:
            mn = minor_of(c.get("static_version"))
            if mn is None:
                notes.append("version not readable, not run: %s" % redact_path(c.get("path")))
            elif mn != rule["minor"]:
                scored.append((BAD, c, "registry says minor %s (not executed); not %s" % (mn, rule["minor"])))
            else:
                notes.append("registry says 3.11 but it could not be run/verified: %s" % redact_path(c.get("path")))
    if not scored:
        return item("python", required, True, status=UNKNOWN, note=("; ".join(notes) or "no usable candidate facts") + errs)
    order = {OK: 0, BAD: 1}
    scored.sort(key=lambda t: order[t[0]])
    st, c, why = scored[0]
    others = ["%s" % (x.get("version") or x.get("static_version")) for _, x, _ in scored[1:]]
    if others or len(real) > 1:
        notes.append("other interpreters seen: %s; seeing several is not a choice; which one to use is an owner decision" % (", ".join(others) or "unreadable"))
    ver = c.get("version") or c.get("static_version")
    if st == OK:
        if c.get("version") == reported:
            notes.append("FINDING: observed %s equals the REPORTED version; not an approval, not an Option B test" % reported)
        else:
            notes.append("FINDING: observed %s differs from the REPORTED %s; meets the minor rule only; not approved" % (c.get("version"), reported))
    return item("python", required, True, ver, c.get("executable") or c.get("path"), st,
                "; ".join(x for x in [why] + notes if x) + errs, approval_for(manifest, "python", c.get("version")))


def pg_service(s):
    if (s.get("exe_name") or "").lower() in ("pg_ctl.exe", "postgres.exe") and "postgresql" in (s.get("file_product_name") or "").lower():
        m = re.match(r"(\d+)\.", s.get("file_product_version") or "")
        if m:
            return int(m.group(1))
    return None


def pg_program(p):
    m = re.match(r"^PostgreSQL\s+(\d+)\b", p.get("display_name") or "")
    v = re.match(r"(\d+)\.", p.get("display_version") or "")
    return int(m.group(1)) if m and v and m.group(1) == v.group(1) else None


def check_database(facts, manifest):
    rule, required = manifest["items"]["database"]["rule"], manifest["items"]["database"]["required"]
    db = facts.get("database")
    if db is None:
        return item("database", required, None, status=UNKNOWN, note="database facts not collected")
    svcs, progs = as_list(db.get("services")), as_list(db.get("programs"))
    drivers = as_list(db.get("driver_entries"))
    drv_note = "; %d client driver/tool entries listed separately and not counted" % len(drivers) if drivers else ""
    listing = ["service '%s' state=%s exe=%s" % (s.get("name"), s.get("state"), s.get("exe_name")) for s in svcs] + \
              ["program '%s' version=%s" % (p.get("display_name"), p.get("display_version")) for p in progs]
    ownership = "presence does not mean it belongs to this project or may be used"
    if not db.get("collection_ok", False):
        return item("database", required, None, status=UNKNOWN,
                    note="collection failed (%s); nothing may be assumed. %s" % (", ".join(as_list(db.get("errors"))) or "no detail", ownership))
    if not svcs and not progs:
        note = "collection succeeded, but detection is by name patterns only: an unlisted product would not appear" + drv_note
        if manifest.get("reported_database_installed"):
            return item("database", required, None, status=UNKNOWN, note=note + ". The owner reports a database IS installed, so this is treated as unknown, not missing")
        return item("database", required, False, status=MISSING, note=note)
    sid = [pg_service(s) for s in svcs]
    pid = [pg_program(p) for p in progs]
    ident = [m for m in sid + pid if m is not None]
    unident = [x for x, m in zip(svcs + progs, sid + pid) if m is None]
    if not ident:
        return item("database", required, True, None, None, UNKNOWN,
                    "database-like entries not positively identified as PostgreSQL: %s. %s%s" % ("; ".join(listing), ownership, drv_note))
    majors = sorted(set(ident))
    notes = []
    if unident:
        notes.append("%d OTHER database-like entr%s NOT identified (forces unknown): %s" % (
            len(unident), "y" if len(unident) == 1 else "ies",
            "; ".join(("service '%s'" % x.get("name")) if "exe_name" in x or "state" in x else ("program '%s'" % x.get("display_name")) for x in unident)))
    if progs and not any(m is not None for m in sid):
        notes.append("an installed-program entry was seen but the service was not seen (not identified from a service, may be stopped, removed or another instance)")
    if len(listing) > 1:
        if len(svcs) == 1 and len(progs) == 1 and len(majors) == 1 and not unident:
            notes.append("the service and the program entry are probably one install seen twice (inferred); whether the project may use it is an owner decision")
        else:
            notes.append("several entries are not a choice; which one the project uses is an owner decision")
    no_service = bool(progs) and not any(m is not None for m in sid)
    if unident or len(majors) > 1 or no_service:
        st = UNKNOWN
    else:
        st = OK if majors == [rule["major"]] else BAD
    return item("database", required, True, "PostgreSQL %s identified" % "/".join("%d.x" % m for m in majors), None, st,
                "; ".join(notes + [ownership + "; never started, connected to or used by this check" + drv_note]),
                approval_for(manifest, "database", None))


def check_wheels(facts, manifest):
    rule, required = manifest["items"]["wheels"]["rule"], manifest["items"]["wheels"]["required"]
    w = facts.get("wheels")
    if w is None:
        return item("wheels", required, None, status=UNKNOWN, note="wheel facts not collected")
    if not w.get("folder_exists"):
        return item("wheels", required, False, status=MISSING)
    fc = w.get("file_count")
    ver = "%s files" % fc
    if not w.get("sha_file_exists") or not w.get("lock_found"):
        return item("wheels", required, True, ver, None, UNKNOWN, "wheels.sha256 or the lock file is absent, cannot verify")
    problems = []
    if w.get("sha_line_count") != rule["count"]:
        problems.append("wheels.sha256 has %s lines, expected %d" % (w.get("sha_line_count"), rule["count"]))
    if fc != rule["count"]:
        problems.append("%s files, expected %d" % (fc, rule["count"]))
    for key, label in (("mismatches", "hash mismatch"), ("missing", "listed but absent"),
                       ("unlisted", "file not listed in wheels.sha256"), ("not_in_lock", "hash not in lock file")):
        vals = as_list(w.get(key))
        if vals:
            problems.append("%s: %s" % (label, ", ".join(vals[:5])))
    note = ("every wheel is listed, matches wheels.sha256 and is in the lock file; wheels.sha256 sha256=%s" % w.get("sha_file_hash")
            if not problems else "; ".join(problems))
    return item("wheels", required, True, ver, None, OK if not problems else BAD, note, NOT_APPROVED)


def check_source(facts, manifest):
    rule, required = manifest["items"]["source"]["rule"], manifest["items"]["source"]["required"]
    s = facts.get("source")
    if s is None:
        return item("source", required, None, status=UNKNOWN, note="source facts not collected")
    if not s.get("main_py_exists"):
        return item("source", required, False, status=MISSING)
    arch = s.get("archive_sha256")
    if arch is None:
        return item("source", required, True, None, None, UNKNOWN,
                    "app/main.py present, NOT verified; %s archive file(s) in archive\\ (exactly one is needed to hash it); the extracted tree is never compared to it" % s.get("archive_count", 0))
    if arch.lower() == rule["archive_sha256"]:
        return item("source", required, True, None, None, UNKNOWN,
                    "archive sha256 matches the expected value; the extracted tree is NOT compared to the archive, so the tree is unverified")
    return item("source", required, True, None, None, BAD, "archive sha256 DIFFERS from the expected value; the tree is unverified")


def classify(facts, manifest):
    return [check_windows(facts, manifest), check_python(facts, manifest), check_database(facts, manifest),
            check_wheels(facts, manifest), check_source(facts, manifest)]


def plan(results, manifest):
    """Text-only plan. Nothing is downloaded or run. Incompatible/unknown items are never replaced."""
    lines = []
    for r in results:
        d = manifest["items"][r["item"]].get("download")
        if r["status"] == OK:
            lines.append("%s: meets the rule as a finding; leave alone; approval is the owner's." % r["item"])
        elif r["status"] == BAD:
            lines.append("%s: found but INCOMPATIBLE. Not valid, not replaced, not modified. Owner decision only; no second download or service start is proposed." % r["item"])
        elif r["status"] == UNKNOWN:
            lines.append("%s: could not be determined. Not treated as valid. Reported, not fixed.%s" % (r["item"], " An existing database-like program is not permission to use it, to download a second one, or to start a service: owner decision only." if r["item"] == "database" else ""))
        elif d is None:
            lines.append("%s: missing, no download item (supplied by the owner/preparer)." % r["item"])
        else:
            lines.append("%s: missing. Candidate: %s. Hash status: %s. Blocker: %s Would need explicit owner approval; this prototype downloads nothing." % (
                r["item"], d["name"], d["hash_status"], d.get("blocker", "none")))
    return lines


def render(results):
    cols = ["item", "required", "found", "version", "path", "compatible", "approval", "status", "note"]
    return [" | ".join("%s=%s" % (c, r[c] if r[c] is not None else "-") for c in cols) for r in results]


def load_facts(arg):
    raw = sys.stdin.buffer.read() if arg == "-" else Path(arg).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encs = ("utf-16",)  # BOM decides LE or BE
    elif raw.startswith(b"\xef\xbb\xbf"):
        encs = ("utf-8-sig",)
    else:
        encs = ("utf-8",)
    for enc in encs:
        try:
            data = json.loads(raw.decode(enc))
        except (UnicodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    raise ValueError("facts are not a JSON object in UTF-8, UTF-8-BOM or UTF-16 (with BOM)")


def summary(results):
    lines = ["COPYABLE SUMMARY (paths redacted, program/service names NOT redacted; nothing was changed or saved):"]
    for r in results:
        lines.append("- %s: %s%s%s" % (r["item"], r["status"].upper(), " (finding only, " + r["approval"] + ")" if r["approval"] != "-" else "",
                                       " | " + str(r["version"]) if r["version"] else ""))
    return lines


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--facts", required=True)
    ap.add_argument("--manifest", default=str(Path(__file__).with_name("requirements_manifest.json")))
    ap.add_argument("--plan", action="store_true")
    a = ap.parse_args(argv)
    try:
        facts = load_facts(a.facts)
    except ValueError as exc:
        print("ERROR: %s" % exc)
        return 2
    manifest = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    results = classify(facts, manifest)
    print("\n".join(render(results)))
    print()
    print("\n".join(summary(results)))
    if a.plan:
        print("\nPLAN (text only, nothing is run or downloaded):")
        print("\n".join(plan(results, manifest)))
    return 0 if all(r["status"] == OK for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
