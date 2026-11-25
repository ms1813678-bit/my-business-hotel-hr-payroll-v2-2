"""Staging-document builder/verifier (Bv3 NEW fixed candidate, document formats v2).

UNVERIFIED against the real repo. Writes only JSON/text documents into a caller-given
directory. It downloads nothing, runs no installer and touches no machine state.
Synthetic data only, Linux.

This module is a document library, NOT the recovered consumer's stage tool: it has no
stage-tree or verify-tree CLI and no verify_install.py, and it claims NO ISS
compatibility from matching event names. What it does, deliberately:

  * Runtime inventory is aligned with the recovered, unchanged ISS consumer contract:
    ok_exit_codes must be exactly [0] (the recovered Pascal's RunChecked accepts only
    exit 0 for every vendor invocation) and silent_args must be a non-empty reviewed
    string (the ISS runs each vendor with exactly those macro-supplied args). Widening
    the exit policy needs a separate explicit ISS source change and tests; nothing here
    invents vendor flags or hashes.
  * NEW strict support for the deliberately selected hashed requirements format:
    exactly `name==version --hash=sha256:<64 lowercase hex>` per line, hashes validated
    for form here and against wheel bytes in verify_hashed_wheelhouse. Pin-only
    resolver input (parse_pins) and hashed install input stay DISTINCT.
  * NEW generated staged-inputs.iss.inc: the macro NAMES (StagedVersion, HasPython,
    PythonFile, PythonSha256, PythonArgs, optional HasWebview2/Webview2File/
    Webview2Sha256/Webview2Args, HasPostgresql/PostgresqlFile/PostgresqlSha256/
    PostgresqlArgs) are the recovered consumer contract read from the recovered
    installer/HOnY-Desktop-Staged.iss; this generator and the file are NEW.
  * NEW wheels.lock.json schema "hony-wheels-lock" v1, labeled NEW: existing consumers
    do not establish the historical wheels.lock schema, so this minimal schema is
    defined, versioned and paired with verify_wheels_lock plus fixtures. It is not
    presented as recovered.

All conclusions are hypotheses until checked against the real repository and on Windows.
"""
import hashlib
import json
import re
from pathlib import Path

SCHEMA = 1
PLAN_FORMAT = "hony-install-plan/v2"               # NEW versioned document format
DEPINV_FORMAT = "hony-dependency-inventory/v2"     # NEW versioned document format
OWN_FORMAT = "hony-ownership-state/v2"             # NEW versioned document format
LOCK_FORMAT = "hony-wheels-lock"                   # NEW schema, NOT a recovered format
LOCK_VERSION = 1
INCLUDE_FORMAT = "hony-staged-inputs-include/v1"   # NEW generator; macro names recovered
NOTE = ("Non-operational planning document. Executes nothing, removes nothing, "
        "does not restore data. Not whole-system atomic.")
RUNTIME_KEYS = ("python", "webview2", "postgresql")
ISS_OK_EXIT_CODES = [0]  # the recovered, unchanged ISS accepts only exit 0 per invocation
HEX64 = re.compile(r"^[0-9a-f]{64}$")
PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)\s*$")
HASHED_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9.!+_-]*)"
    r"[ \t]+--hash=sha256:([0-9a-f]{64})[ \t]*$")
# NEW strict allowlists (hypotheses, UNVERIFIED against the real repo). The vendor filename and
# silent args are spliced by the recovered ISS into (a) ISPP double-quoted #define strings,
# (b) Pascal single-quoted literals ('{#PythonFile}', '{#PythonArgs} TargetDir="'), and
# (c) an Inno [Files] Source "..." path. Rather than guess escaping for those contexts, anything
# outside a conservative character set is refused; no escaping is attempted.
VENDOR_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
SILENT_ARGS_RE = re.compile(r"^[A-Za-z0-9/_=.,:+-]+( [A-Za-z0-9/_=.,:+-]+)*$")
SILENT_ARGS_MAX = 256
WIN_DEVICE_STEMS = ({"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)}
                    | {"LPT%d" % i for i in range(1, 10)})
# strict wheel file name: name-version[-build]-pythontag-abitag-platformtag.whl, one safe charset
WHEEL_PART = r"[A-Za-z0-9][A-Za-z0-9_.+!]*"
WHEEL_FILE_RE = re.compile(r"^(%s)-(%s)(?:-(\d[A-Za-z0-9_.]*))?-(%s)-(%s)-(%s)\.whl$"
                           % (WHEEL_PART, WHEEL_PART, WHEEL_PART, WHEEL_PART, WHEEL_PART))
VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.!+-]{0,63}$")


class StageError(Exception):
    pass


def check_vendor_filename(key, fn):
    """Bare vendor basename only. Refuses quotes, apostrophes, braces, control chars, spaces,
    separators, colons and every other metacharacter by allowlist; also trailing dot and
    Windows device names. Runs before any include text is produced."""
    if (not isinstance(fn, str) or VENDOR_FILENAME_RE.fullmatch(fn) is None or fn.endswith(".")
            or fn.split(".", 1)[0].upper() in WIN_DEVICE_STEMS):
        raise StageError(f"{key}: filename must be a bare vendor file name from the safe "
                         "character set (letters, digits, . _ + -)")


def check_silent_args(key, args):
    """Allowlist for text that the ISS embeds in a Pascal single-quoted literal, an ISPP
    double-quoted string and a command line. No quotes, apostrophes, braces, backslashes,
    control chars, percent, semicolons or other metacharacters; no escaping is attempted."""
    if (not isinstance(args, str) or len(args) > SILENT_ARGS_MAX
            or SILENT_ARGS_RE.fullmatch(args) is None):
        raise StageError(f"{key}: silent_args outside the reviewed safe character set "
                         "(letters, digits, / _ = . , : + - and single spaces)")


def validate_inventory(inventory):
    seen = set()
    for it in inventory:
        k = it.get("key")
        if k not in RUNTIME_KEYS:
            raise StageError(f"unknown runtime key {k!r}")
        if k in seen:
            raise StageError(f"duplicate runtime key {k!r}")
        seen.add(k)
        fn = it.get("filename", "")
        check_vendor_filename(k, fn)
        if not HEX64.fullmatch(str(it.get("sha256", "")).lower()):
            raise StageError(f"{k}: sha256 must be 64 hex characters")
        if it.get("ok_exit_codes") != ISS_OK_EXIT_CODES:
            raise StageError(f"{k}: ok_exit_codes must be exactly [0]: the unchanged ISS "
                             "accepts only exit code 0 for each vendor invocation; widening "
                             "the exit policy needs a separate explicit ISS source change and tests")
        args = it.get("silent_args")
        if not isinstance(args, str) or not args.strip():
            raise StageError(f"{k}: silent_args must be a non-empty reviewed string; the ISS "
                             "runs the vendor with exactly these args")
        check_silent_args(k, args)
    if "python" not in seen:
        raise StageError("inventory has no pinned python installer")


def build_install_plan(inventory):
    validate_inventory(inventory)
    steps = []
    for it in inventory:
        steps.append({"id": f"install-{it['key']}", "effects": [
            {"kind": "runtime", "component": it["key"], "reversible": it["key"] == "python"},
            {"kind": "machine-state", "reason": "vendor installer registry/services", "reversible": False},
        ]})
    steps += [
        {"id": "create-venv", "effects": [{"kind": "dir", "path": "{app}\\.venv", "reversible": True}]},
        {"id": "install-wheels", "effects": [{"kind": "inside-created-dir", "path": "{app}\\.venv", "reversible": True}]},
        {"id": "verify-install", "effects": []},
    ]
    return {"schema": SCHEMA, "format": PLAN_FORMAT, "operational": False, "note": NOTE,
            "atomicity": "per-step-compensation-only (planned, not executed)",
            "steps": steps}


def ownership_template():
    return {"schema": SCHEMA, "format": OWN_FORMAT, "operational": False,
            "components": {k: {"ownership": "unknown", "evidence": {}} for k in RUNTIME_KEYS}}


def dependency_inventory(inventory):
    validate_inventory(inventory)
    return {"schema": SCHEMA, "format": DEPINV_FORMAT, "operational": False, "note": NOTE,
            "standalone_operational_installer": False,
            "unimplemented_setup": ["HOnYBackend service registration", "PostgreSQL provisioning and migrations",
                                    ".env creation", "certificates and firewall"],
            "runtimes": [{k: it[k] for k in ("key", "filename", "sha256", "ok_exit_codes", "silent_args")}
                         for it in inventory]}


# ------------------------------------------------------------------ pins (resolver input)
def parse_pins(text):
    """Pins from a uv-compiled requirements file; comment/continuation lines ignored.
    A non-comment line that is not an exact `name==version` pin raises.
    DISTINCT from the hashed install input (parse_hashed_requirements)."""
    pins = {}
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = PIN_RE.match(s)
        if not m:
            raise StageError(f"line {n}: not an exact pin")
        name = re.sub(r"[-_.]+", "-", m.group(1)).lower()
        if name in pins:
            raise StageError(f"line {n}: duplicate pin {name}")
        pins[name] = m.group(2)
    return pins


# ------------------------------------------- hashed requirements (NEW install input)
def parse_hashed_requirements(text):
    """NEW: strict parser for the deliberately selected hashed install input:
    exactly `name==version --hash=sha256:<64 lowercase hex>` per non-comment line.
    No URLs, no pip directives, no other options, no extras, no duplicate names.
    Hash FORM is validated here; wheel BYTES are checked by verify_hashed_wheelhouse."""
    reqs = {}
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        m = HASHED_RE.match(s)
        if not m:
            raise StageError(f"line {n}: not an exact `name==version --hash=sha256:<64hex>` line")
        name = re.sub(r"[-_.]+", "-", m.group(1)).lower()
        if name in reqs:
            raise StageError(f"line {n}: duplicate requirement {name}")
        reqs[name] = {"version": m.group(2), "sha256": m.group(3)}
    if not reqs:
        raise StageError("no requirements found")
    return reqs


def wheel_pin(filename):
    """Strict: a bare wheel file name only (no separators, drive, traversal, control chars),
    with python/abi/platform tags. Pure string check, no IO."""
    if not isinstance(filename, str) or len(filename) > 255:
        raise StageError("not a wheel file name: bad type or length")
    m = WHEEL_FILE_RE.fullmatch(filename)
    if not m:
        raise StageError(f"not a wheel file name (strict bare name required): {filename!r}")
    return re.sub(r"[-_.]+", "-", m.group(1)).lower(), m.group(2)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def check_wheelhouse(pins, wheel_names):
    """Every pin has exactly one wheel with that version; no unpinned wheel. Returns problem list."""
    problems, found = [], {}
    for w in wheel_names:
        try:
            n, v = wheel_pin(w)
        except StageError as e:
            problems.append(str(e))
            continue
        if n in found:
            problems.append(f"two wheels for {n}")
        found[n] = v
    for n, v in pins.items():
        if n not in found:
            problems.append(f"missing wheel for {n}=={v}")
        elif found[n] != v:
            problems.append(f"{n}: wheel {found[n]} != pin {v}")
    for n in found:
        if n not in pins:
            problems.append(f"unpinned wheel {n}")
    return problems


def verify_hashed_wheelhouse(reqs, wheelhouse_dir):
    """NEW: every hashed requirement has exactly one wheel with matching name, version
    AND sha256 (re-hashed from bytes); no unpinned wheels. Returns problem list."""
    problems, found = [], {}
    d = Path(wheelhouse_dir)
    names = sorted(p.name for p in d.glob("*.whl")) if d.is_dir() else []
    if not names:
        problems.append(f"no wheels found in {d}")
    for w in names:
        try:
            n, v = wheel_pin(w)
        except StageError as e:
            problems.append(str(e))
            continue
        if n in found:
            problems.append(f"two wheels for {n}")
        found[n] = (v, w)
    for n, r in reqs.items():
        if n not in found:
            problems.append(f"missing wheel for {n}=={r['version']}")
        else:
            v, w = found[n]
            if v != r["version"]:
                problems.append(f"{n}: wheel {v} != pinned {r['version']}")
            elif (d / w).is_symlink() or not (d / w).is_file():
                problems.append(f"{n}: {w} is not a regular non-symlink file")
            elif sha256_file(d / w) != r["sha256"]:
                problems.append(f"{n}: sha256 mismatch for {w}")
    for n in found:
        if n not in reqs:
            problems.append(f"unpinned wheel {n}")
    return problems


# ------------------------------------------------------ NEW wheels.lock schema (v1)
def build_wheels_lock(reqs, wheelhouse_dir):
    """NEW schema document, labeled NEW: NOT a recovered format. Fails unless the
    wheelhouse exactly matches the hashed requirements (verify_hashed_wheelhouse)."""
    problems = verify_hashed_wheelhouse(reqs, wheelhouse_dir)
    if problems:
        raise StageError("wheelhouse does not match hashed requirements: " + "; ".join(problems))
    d = Path(wheelhouse_dir)
    wheels = []
    for w in sorted(p.name for p in d.glob("*.whl")):
        n, v = wheel_pin(w)
        wheels.append({"name": n, "version": v, "filename": w, "sha256": reqs[n]["sha256"]})
    return {"schema": LOCK_FORMAT, "version": LOCK_VERSION,
            "status": "NEW - not a recovered format",
            "operational": False, "note": NOTE,
            "wheels": wheels}


def verify_wheels_lock(doc, wheelhouse_dir=None):
    """NEW verifier paired with the NEW schema. Structural checks always (no IO); when
    wheelhouse_dir is given, re-hashes the listed files only after the filename passed the
    strict bare-name check, and only for a regular non-symlink file contained in the
    wheelhouse. Returns problem list."""
    problems = []
    if not isinstance(doc, dict) or doc.get("schema") != LOCK_FORMAT:
        return ["not a hony-wheels-lock document"]
    if doc.get("version") != LOCK_VERSION or isinstance(doc.get("version"), bool):
        problems.append(f"unsupported version {doc.get('version')!r}")
    wheels = doc.get("wheels")
    if not isinstance(wheels, list) or not wheels:
        problems.append("wheels must be a non-empty list")
        wheels = []
    seen_names, seen_files = set(), set()
    root = None
    if wheelhouse_dir is not None:
        root = Path(wheelhouse_dir)
        if not root.is_dir() or root.is_symlink():
            problems.append("wheelhouse is not a regular directory")
            root = None
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
            n, v = wheel_pin(w["filename"])  # strict bare name; rejects before any IO
        except StageError as e:
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
        if root is not None:
            p = root / w["filename"]
            if p.is_symlink() or not p.is_file():
                problems.append(f"wheels[{i}]: missing or not a regular non-symlink file")
            elif p.resolve().parent != root.resolve():
                problems.append(f"wheels[{i}]: not contained in the wheelhouse")
            elif sha256_file(p) != w["sha256"]:
                problems.append(f"wheels[{i}]: sha256 mismatch")
    return problems


# ------------------------------------------- NEW staged-inputs.iss.inc generator
def generate_iss_include(inventory, staged_version):
    """NEW generator for StageRoot/staged-inputs.iss.inc. Macro NAMES are the recovered
    consumer contract from installer/HOnY-Desktop-Staged.iss; the generator is NEW.
    Defines optional WebView2/PostgreSQL macros only when the runtime is staged."""
    validate_inventory(inventory)
    if not VERSION_RE.match(str(staged_version)):
        raise StageError("bad staged version")
    by_key = {it["key"]: it for it in inventory}
    lines = ["; staged-inputs.iss.inc - NEW generated include (%s); UNVERIFIED, synthetic data only" % INCLUDE_FORMAT,
             '#define StagedVersion "%s"' % staged_version]

    def emit(key, macro):
        it = by_key.get(key)
        if it is None:
            return
        lines.append("#define Has%s" % macro)
        lines.append('#define %sFile "%s"' % (macro, it["filename"]))
        lines.append('#define %sSha256 "%s"' % (macro, str(it["sha256"]).lower()))
        lines.append('#define %sArgs "%s"' % (macro, it["silent_args"]))

    emit("python", "Python")
    emit("webview2", "Webview2")
    emit("postgresql", "Postgresql")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ input verification
def verify_inputs(inputs_dir, inventory):
    validate_inventory(inventory)
    problems = []
    for it in inventory:
        p = Path(inputs_dir) / it["filename"]
        if not p.is_file() or p.is_symlink():
            problems.append(f"{it['key']}: input missing or not a regular file")
        elif sha256_file(p) != it["sha256"].lower():
            problems.append(f"{it['key']}: sha256 mismatch")
    return problems


def write_documents(out_dir, inventory, staged_version=None, hashed_requirements=None,
                    wheelhouse_dir=None):
    """Writes the three JSON planning documents. When staged_version is given, also
    writes the NEW staged-inputs.iss.inc. When hashed_requirements (text) is given,
    also writes the NEW wheels.lock.json (requires wheelhouse_dir with the wheels)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    docs = {"install-plan.json": build_install_plan(inventory),
            "ownership-state.template.json": ownership_template(),
            "dependency-inventory.json": dependency_inventory(inventory)}
    names = []
    for name, doc in docs.items():
        (out / name).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        names.append(name)
    if staged_version is not None:
        (out / "staged-inputs.iss.inc").write_text(
            generate_iss_include(inventory, staged_version), encoding="utf-8")
        names.append("staged-inputs.iss.inc")
    if hashed_requirements is not None:
        if wheelhouse_dir is None:
            raise StageError("wheels.lock.json requires wheelhouse_dir with the wheel files")
        lock = build_wheels_lock(parse_hashed_requirements(hashed_requirements), wheelhouse_dir)
        (out / "wheels.lock.json").write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n",
                                              encoding="utf-8")
        names.append("wheels.lock.json")
    return sorted(names)
