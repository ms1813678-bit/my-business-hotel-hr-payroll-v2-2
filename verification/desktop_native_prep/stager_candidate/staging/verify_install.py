"""NEW read-only installed-distribution verifier, paired with the NEW wheels.lock schema.

UNVERIFIED against the real repo. Hypothesis until checked against the real repository
and run natively on Windows. The recovered, unchanged ISS invokes this file in isolated
mode:

    python -I verify_install.py "<app>\\installer-checks\\wheels.lock.json"

and accepts only exit code 0. This script is therefore fully self-contained: standard
library only, no sibling imports (it must work under -I with no path setup).

Contract (from the 0335 interface brief):

  * READ-ONLY. It imports no application code, reads no app config, no .env, no
    database and no network. It only reads the lock file it is given and queries
    importlib.metadata for installed distributions.
  * Verifies installed distribution names/versions against the NEW "hony-wheels-lock"
    v1 lock: rejects missing distributions, version mismatches, duplicate
    distributions and canonical-name conflicts (two installed names that
    canonicalize to the same key, e.g. "Foo_Bar" and "foo-bar").
  * Verifies the wheel-origin lock STRUCTURE: every entry's filename must be a strict
    wheel file name that agrees with its canonical name/version, sha256 must be 64
    lowercase hex, no duplicate names/filenames. This checks the lock's shape only;
    it does NOT and CANNOT re-derive the original wheel archive sha256 from an
    installed directory, and it never compares an installed package tree hash to a
    wheel archive hash. Byte integrity against the original wheels would need a
    separately generated wheel-member manifest with its own schema and tests; that
    is a writer decision and is NOT part of this lock schema (see not_checkable).
  * Verifies the runtime target (Windows / CPython 3.11 / x64) in native mode; the
    environment is injectable for unit tests (verify_installed env parameter).
  * Native UI/SCM state is not covered.

Output: one JSON document on stdout (format hony-verify-install-result/v1) with
status, problems, checked, not_checkable and non_operational=true. Exit 0 on pass,
exit 2 on any refusal/failure (the ISS treats any nonzero as failure).
"""
import json
import re
import struct
import sys

LOCK_FORMAT = "hony-wheels-lock"   # NEW schema, NOT a recovered format
LOCK_VERSION = 1
RESULT_FORMAT = "hony-verify-install-result/v1"
NOTE = ("Read-only verification. Imports no application code; touches no config, "
        "database or network; changes nothing.")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
_WHEEL_PART = r"[A-Za-z0-9][A-Za-z0-9_.+!]*"
WHEEL_FILE_RE = re.compile(r"^(%s)-(%s)(?:-(\d[A-Za-z0-9_.]*))?-(%s)-(%s)-(%s)\.whl$"
                           % ((_WHEEL_PART,) * 5))

TARGET = {"platform": "win32", "implementation": "CPython",
          "major": 3, "minor": 11, "bits": 64, "machine": "AMD64"}

CHECKED = [
    "wheels.lock schema, version and entry structure (wheel-origin structure)",
    "strict wheel filename agrees with canonical name/version for every entry",
    "installed presence and exact version of every locked distribution "
    "(importlib.metadata)",
    "no duplicate installed distributions per canonical name; no canonical-name "
    "conflicts",
    "runtime target: Windows / CPython 3.11 / 64-bit x64",
]
NOT_CHECKABLE = [
    "original wheel archive sha256 cannot be re-derived from an installed "
    "directory; no installed-tree hash is ever compared to a wheel archive hash",
    "per-file byte integrity against the original wheels (needs a separately "
    "generated wheel-member manifest with its own schema and tests; not part of "
    "this lock schema)",
    "vendor runtime installers (python/webview2/postgresql) are not Python "
    "distributions and are not visible to importlib.metadata",
    "native UI/SCM state",
]


class VerifyError(Exception):
    pass


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def wheel_pin(filename):
    """Strict bare wheel file name; pure string check, no IO."""
    if not isinstance(filename, str) or len(filename) > 255:
        raise VerifyError("not a wheel file name: bad type or length")
    m = WHEEL_FILE_RE.fullmatch(filename)
    if not m:
        raise VerifyError(f"not a wheel file name (strict bare name required): {filename!r}")
    return canonical(m.group(1)), m.group(2)


def check_lock_structure(doc):
    """Structural checks on the NEW lock document. No filesystem IO beyond the read
    the caller already did."""
    problems = []
    if not isinstance(doc, dict) or doc.get("schema") != LOCK_FORMAT:
        return [f"not a {LOCK_FORMAT} document"]
    if doc.get("version") != LOCK_VERSION or isinstance(doc.get("version"), bool):
        problems.append(f"unsupported lock version {doc.get('version')!r}")
    wheels = doc.get("wheels")
    if not isinstance(wheels, list) or not wheels:
        problems.append("wheels must be a non-empty list")
        return problems
    seen_names, seen_files = set(), set()
    for i, w in enumerate(wheels):
        if not isinstance(w, dict) or set(w) != {"name", "version", "filename", "sha256"}:
            problems.append(f"wheels[{i}]: unexpected fields")
            continue
        if not all(isinstance(w[k], str) for k in ("name", "version", "filename", "sha256")):
            problems.append(f"wheels[{i}]: all fields must be strings")
            continue
        if HEX64.fullmatch(w["sha256"]) is None:
            problems.append(f"wheels[{i}]: bad sha256")
        try:
            n, v = wheel_pin(w["filename"])
        except VerifyError as e:
            problems.append(f"wheels[{i}]: {e}")
            continue
        if n != w["name"] or v != w["version"]:
            problems.append(f"wheels[{i}]: filename disagrees with canonical name/version")
            continue
        if n in seen_names:
            problems.append(f"wheels[{i}]: duplicate canonical name {n}")
            continue
        seen_names.add(n)
        if w["filename"] in seen_files:
            problems.append(f"wheels[{i}]: duplicate filename")
            continue
        seen_files.add(w["filename"])
    return problems


def runtime_env():
    """The actual runtime environment. Native mode only."""
    import platform as _platform
    return {"platform": sys.platform,
            "implementation": _platform.python_implementation(),
            "major": sys.version_info[0], "minor": sys.version_info[1],
            "bits": struct.calcsize("P") * 8,
            "machine": _platform.machine()}


def check_runtime_target(env):
    """Verify the runtime target Windows/CPython3.11/x64. `env` is injectable for
    unit tests; native callers use runtime_env()."""
    problems = []
    if env.get("platform") != TARGET["platform"]:
        problems.append(f"platform {env.get('platform')!r} != {TARGET['platform']!r} "
                        "(native Windows target required)")
    if env.get("implementation") != TARGET["implementation"]:
        problems.append(f"implementation {env.get('implementation')!r} != CPython")
    if (env.get("major"), env.get("minor")) != (TARGET["major"], TARGET["minor"]):
        problems.append(f"Python {env.get('major')}.{env.get('minor')} != "
                        f"{TARGET['major']}.{TARGET['minor']}")
    if env.get("bits") != TARGET["bits"]:
        problems.append(f"{env.get('bits')}-bit interpreter != 64-bit")
    if str(env.get("machine", "")).upper() != TARGET["machine"]:
        problems.append(f"machine {env.get('machine')!r} != {TARGET['machine']!r} (x64)")
    return problems


def installed_distributions():
    """Default provider: the real environment via importlib.metadata. Returns a list
    of {name, version} records. Read-only."""
    from importlib import metadata as importlib_metadata
    out = []
    for dist in importlib_metadata.distributions():
        try:
            name = dist.metadata["Name"]
        except Exception:  # malformed metadata: report, never crash
            name = None
        if not name:
            out.append({"name": None, "version": getattr(dist, "version", None)})
            continue
        out.append({"name": name, "version": dist.version})
    return out


def verify_installed(lock_doc, dist_records=None, env=None):
    """Verify installed distributions against the NEW lock. `dist_records` and `env`
    are injectable for unit tests; when None, the real environment is queried.
    Returns the report dict."""
    problems = list(check_lock_structure(lock_doc))
    if env is None:
        env = runtime_env()
    problems += check_runtime_target(env)
    extras = []
    if dist_records is None:
        dist_records = installed_distributions()
    by_canon = {}
    for d in dist_records:
        if not isinstance(d, dict) or not isinstance(d.get("name"), str):
            problems.append("installed distribution with missing/malformed name")
            continue
        c = canonical(d["name"])
        if c in by_canon:
            problems.append(f"duplicate/canonical conflict: {by_canon[c]['name']!r} and "
                            f"{d['name']!r} canonicalize to {c!r}")
            continue
        by_canon[c] = d
    wheels = lock_doc.get("wheels") if isinstance(lock_doc, dict) else None
    for w in wheels or []:
        if not isinstance(w, dict) or "name" not in w or "version" not in w:
            continue  # already reported by check_lock_structure
        d = by_canon.pop(w["name"], None)
        if d is None:
            problems.append(f"missing installed distribution {w['name']}=={w['version']}")
        elif d.get("version") != w["version"]:
            problems.append(f"version mismatch for {w['name']}: installed "
                            f"{d.get('version')!r} != locked {w['version']!r}")
    extras = sorted(d["name"] for d in by_canon.values())
    return {"format": RESULT_FORMAT, "version": 1,
            "status": "refused" if problems else "pass",
            "problems": problems,
            "checked": list(CHECKED),
            "not_checkable": list(NOT_CHECKABLE),
            "extras_not_checked": extras,
            "non_operational": True, "note": NOTE,
            "unverified": "UNVERIFIED against the real repo; hypothesis until checked "
                          "natively on Windows"}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print(json.dumps({"format": RESULT_FORMAT, "version": 1, "status": "refused",
                          "problems": ["usage: verify_install.py wheels.lock.json"],
                          "checked": [], "not_checkable": list(NOT_CHECKABLE),
                          "non_operational": True, "note": NOTE}, indent=2, sort_keys=True))
        return 2
    try:
        with open(argv[0], "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        print(json.dumps({"format": RESULT_FORMAT, "version": 1, "status": "refused",
                          "problems": [f"lock unreadable: {e.__class__.__name__}: {e}"],
                          "checked": [], "not_checkable": list(NOT_CHECKABLE),
                          "non_operational": True, "note": NOTE}, indent=2, sort_keys=True))
        return 2
    report = verify_installed(doc)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    sys.exit(main())
