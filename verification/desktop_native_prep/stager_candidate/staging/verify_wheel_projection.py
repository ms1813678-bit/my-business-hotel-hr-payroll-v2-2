"""NEW standalone read-only wheel-payload projection verifier (NOT a recovered format).

UNVERIFIED against the real repo. Hypothesis until checked against the real
repository and run natively on Windows.

Consumer (0404 brief):

    python -I verify_wheel_projection.py PROJECTION_JSON SITE_PACKAGES_ROOT

This file is therefore fully self-contained: standard library only, no sibling
imports (it must work under -I with no path setup). Exit 0 on pass, exit 2 on
any refusal/failure, exactly one JSON report on stdout
(format hony-wheel-projection-verify/v1).

Contract:

  * READ-ONLY. Imports no application code, reads no app config, no .env, no
    database and no network. It reads only the projection manifest and the
    files the manifest names under the EXPLICIT site-packages root argument.
    It writes nothing.
  * Validates the manifest schema and EVERY target path BEFORE reading any
    payload. Missing/tampered/aliased/path-schema problems all refuse.
  * The site-packages root comes ONLY from the command line, never from the
    manifest: root selection belongs to the writer/native interpreter and is
    never inferred from a (possibly malicious) manifest. The root must exist,
    be a real directory and not a symlink.
  * Every projected file is checked as: non-symlink real ancestors (walked
    component by component from the root), a non-symlink regular leaf,
    resolved containment inside the resolved root, then size and sha256 of
    the actual bytes.
  * Reports NOT_COVERED/transformed members from the manifest (e.g.
    .data/scripts of xlrd/bottle/pywin32) and the path-identity limits.
    Extra installed files are outside this projection: this is ORIGINAL
    PAYLOAD PROJECTION INTEGRITY, not an exact full installed tree and not a
    malware/runtime security approval.
"""
import hashlib
import json
import os
import re
import stat
import sys

PROJECTION_FORMAT = "hony-wheel-projection"   # NEW schema, NOT a recovered format
PROJECTION_VERSION = 1
RESULT_FORMAT = "hony-wheel-projection-verify/v1"

NOTE = ("Read-only verification. Imports no application code; touches no config, "
        "database, network or environment; writes nothing.")
SCOPE_LIMIT = ("Original wheel payload projection integrity only. Extra installed "
               "files are outside this projection: this is NOT an exact full "
               "installed-tree check, NOT whole-distribution integrity and NOT a "
               "malware or runtime security approval.")
PATH_IDENTITY_LIMIT = ("Lexical path checks plus symlink/containment resolution "
                       "only. Windows junction/reparse-point/8.3-short-name "
                       "identity and time-of-check/time-of-use races are NOT "
                       "proved by these checks.")

CHECKED = [
    "projection manifest schema, version and field types (hony-wheel-projection v1)",
    "projection content hash consistency (recomputed; consistency, not authenticity)",
    "every wheel origin: strict wheel file name agreeing with canonical "
    "name/version; no duplicate or casefold-aliased wheels",
    "nonempty coverage: no empty wheel entries; at least one covered file overall",
    "pre-IO bounds: entry counts, per-member and aggregate expected sizes",
    "every target path: strict relative-path schema, validated before any payload read",
    "no duplicate or casefold-aliased targets or members inside the manifest",
    "explicit empty directory entries: safe paths, unique, never payload targets",
    "site-packages root from the command line only: existing, real directory, non-symlink",
    "every projected file: non-symlink real ancestors, non-symlink regular leaf, "
    "resolved containment inside the resolved root",
    "on-disk size (stat) against the projection BEFORE hashing; then size and "
    "sha256 of every projected file's actual bytes",
]
NOT_CHECKABLE = [
    "transformed/non-site members listed as not_covered in the manifest (e.g. "
    ".data/scripts, .data/headers, .data/data, RECORD, bytecode) are reported, "
    "not verified",
    "extra installed files outside the projection (bytecode, entry-point "
    "wrappers, INSTALLER/REQUESTED/direct_url.json, runtime-generated files)",
    "Windows junction/reparse-point/8.3-short-name path identity and "
    "time-of-check/time-of-use races (lexical + symlink checks only)",
    "authenticity of the manifest itself (no signature; hash is self-consistency)",
    "whole-distribution / flat-copy whole-install integrity (never claimed)",
    "malware or runtime security approval (never claimed)",
]

HEX64 = re.compile(r"^[0-9a-f]{64}$")
DEVICE_NAMES = ({"CON", "PRN", "AUX", "NUL"}
                | {f"COM{i}" for i in range(1, 10)}
                | {f"LPT{i}" for i in range(1, 10)})

# Strict wheel file name for origin validation (same rule as the lock side).
_WHEEL_PART = r"[A-Za-z0-9][A-Za-z0-9_.+!]*"
WHEEL_FILE_RE = re.compile(r"^(%s)-(%s)(?:-(\d[A-Za-z0-9_.]*))?-(%s)-(%s)-(%s)\.whl$"
                           % ((_WHEEL_PART,) * 5))

# Resource bounds, mirrored from the builder's recorded defaults. Enforced on
# manifest EXPECTED sizes BEFORE any payload read; a manifest asking to hash
# more than this refuses without touching the tree.
MAX_MEMBERS_PER_WHEEL = 20000
MAX_TOTAL_ENTRIES = 200000
MAX_MEMBER_BYTES = 256 * 1024 * 1024
MAX_AGGREGATE_BYTES = 4 * 1024 * 1024 * 1024


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def wheel_pin(filename):
    """Strict bare wheel file name; pure string check, no IO. Raises VerifyError."""
    if not isinstance(filename, str) or len(filename) > 255:
        raise VerifyError("not a wheel file name: bad type or length")
    m = WHEEL_FILE_RE.fullmatch(filename)
    if not m:
        raise VerifyError(f"not a strict wheel file name: {filename!r}")
    return canonical(m.group(1)), m.group(2)


class VerifyError(Exception):
    pass


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


def validate_relative_path(name):
    """Same NEW strict relative POSIX-path policy as the builder. Returns the
    component list or raises VerifyError. Lexical only."""
    if not isinstance(name, str) or not name:
        raise VerifyError("empty or not a string")
    if len(name) > 512:
        raise VerifyError(f"overlong path ({len(name)} chars)")
    if "\x00" in name:
        raise VerifyError("NUL character")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise VerifyError("control character")
    if "\\" in name:
        raise VerifyError("backslash separator")
    if name.startswith("/"):
        raise VerifyError("absolute path")
    if ":" in name:
        raise VerifyError("colon (drive letter or alternate data stream)")
    comps = name.split("/")
    for c in comps:
        if not c:
            raise VerifyError("empty component")
        if c == ".":
            raise VerifyError("dot component")
        if c == "..":
            raise VerifyError("parent-directory traversal")
        if c.endswith(".") or c.endswith(" "):
            raise VerifyError(f"trailing dot or space in component {c!r}")
        stem = c.split(".", 1)[0].upper()
        if stem in DEVICE_NAMES:
            raise VerifyError(f"reserved device name {c!r}")
    return comps


def map_member(name):
    """The NEW mapping boundary. Returns (target, None) for covered members or
    (None, reason) for NOT_COVERED members. Never raises: every name reaching
    this point already passed validate_relative_path."""
    comps = name.split("/")
    first = comps[0]
    if first.endswith(".data") and len(comps) >= 2:
        scheme = comps[1]
        rest = "/".join(comps[2:])
        if scheme in ("purelib", "platlib") and rest:
            return rest, None
        if scheme in ("scripts", "headers", "data"):
            return None, (f".data/{scheme} members are transformed at install time "
                          f"(rewrite/wrapper/other-root); not byte-preserved "
                          f"site-packages payload, not verified")
        return None, (f"unknown .data scheme {scheme!r}; not byte-preserved "
                      f"site-packages payload, not verified")
    if first.endswith(".dist-info") and len(comps) == 2:
        base = comps[1]
        if base == "RECORD":
            return None, ("RECORD is rewritten at install time; not original "
                          "payload, not verified")
        if base in {"INSTALLER", "REQUESTED", "direct_url.json"}:
            return None, (f"{base} is generated by the installer, not original "
                          f"wheel payload; not verified")
        return name, None
    if name.endswith(".pyc"):
        return None, "bytecode is runtime-generated; not claimed covered"
    return name, None



def validate_manifest(doc):
    """Schema/type/bool/target-schema validation. Returns a problem list.
    Every target path, every wheel origin, all entry counts and all expected
    sizes are validated here, BEFORE any payload read."""
    problems = []
    if not isinstance(doc, dict) or doc.get("schema") != PROJECTION_FORMAT:
        return [f"not a {PROJECTION_FORMAT} document"]
    if doc.get("version") != PROJECTION_VERSION or isinstance(doc.get("version"), bool):
        problems.append(f"unsupported projection version {doc.get('version')!r}")
    if not isinstance(doc.get("projection_sha256"), str) or \
            HEX64.fullmatch(doc.get("projection_sha256") or "") is None:
        problems.append("projection_sha256 missing or not 64 lowercase hex")
    wheels = doc.get("wheels")
    if not isinstance(wheels, list) or not wheels:
        problems.append("wheels must be a non-empty list")
        return problems
    seen_targets = {}
    seen_wheels = {}
    total_entries = 0
    total_expected = 0
    total_members_all = 0
    for i, w in enumerate(wheels):
        tag = f"wheels[{i}]"
        if not isinstance(w, dict) or not {"filename", "name", "version",
                                           "archive_sha256", "members",
                                           "not_covered"} <= set(w):
            problems.append(f"{tag}: missing required fields")
            continue
        for k in ("filename", "name", "version"):
            if not isinstance(w[k], str):
                problems.append(f"{tag}: {k} must be a string")
        # Strict origin: the wheel this projection claims to come from must be
        # a strict wheel file name agreeing with its canonical name/version,
        # exactly as the lock side enforces. A traversal-like or arbitrary
        # origin string refuses even when there is nothing to verify.
        if all(isinstance(w[k], str) for k in ("filename", "name", "version")):
            try:
                n, v = wheel_pin(w["filename"])
                if n != w["name"] or v != w["version"]:
                    problems.append(
                        f"{tag}: filename disagrees with canonical name/version "
                        f"({w['filename']!r} vs {w['name']!r}/{w['version']!r})")
            except VerifyError as e:
                problems.append(f"{tag}: bad origin filename: {e}")
            wcf = w["filename"].casefold()
            if wcf in seen_wheels:
                problems.append(f"{tag}: duplicate/casefold-aliased wheel "
                                f"{w['filename']!r} (also at {seen_wheels[wcf]})")
            else:
                seen_wheels[wcf] = tag
        if not isinstance(w["archive_sha256"], str) or \
                HEX64.fullmatch(w["archive_sha256"]) is None:
            problems.append(f"{tag}: archive_sha256 not 64 lowercase hex")
        if not isinstance(w["members"], list) or not isinstance(w["not_covered"], list):
            problems.append(f"{tag}: members/not_covered must be lists")
            continue
        directories = w.get("directories", [])
        if not isinstance(directories, list):
            problems.append(f"{tag}: directories must be a list when present")
            directories = []
        # Nonempty coverage: an entry claiming a wheel but covering and
        # reporting nothing is anomalous; a pass over it would verify nothing.
        if not w["members"] and not w["not_covered"]:
            problems.append(f"{tag}: empty wheel entry (no members and no "
                            f"not_covered; a pass would verify nothing)")
        entry_count = len(w["members"]) + len(w["not_covered"]) + len(directories)
        if entry_count > MAX_MEMBERS_PER_WHEEL:
            problems.append(f"{tag}: entry count {entry_count} exceeds bound "
                            f"{MAX_MEMBERS_PER_WHEEL} (pre-IO)")
        total_entries += entry_count
        if total_entries > MAX_TOTAL_ENTRIES:
            problems.append(f"total entry count exceeds bound {MAX_TOTAL_ENTRIES} "
                            f"(pre-IO)")
        seen_members = {}
        seen_dirs = {}
        for j, dname in enumerate(directories):
            dtag = f"{tag}.directories[{j}]"
            if not isinstance(dname, str) or not dname.endswith("/"):
                problems.append(f"{dtag}: directory name must be a string with "
                                f"a trailing slash")
                continue
            try:
                validate_relative_path(dname[:-1])
            except VerifyError as e:
                problems.append(f"{dtag}: bad directory path: {e}")
            dcf = dname[:-1].casefold()
            if dcf in seen_dirs or dcf in seen_members:
                problems.append(f"{dtag}: duplicate/colliding directory {dname!r}")
            else:
                seen_dirs[dcf] = dtag
        for j, m in enumerate(w["members"]):
            mtag = f"{tag}.members[{j}]"
            if not isinstance(m, dict) or set(m) != {"member", "target", "sha256", "size"}:
                problems.append(f"{mtag}: unexpected fields")
                continue
            if not isinstance(m["member"], str) or not isinstance(m["target"], str):
                problems.append(f"{mtag}: member/target must be strings")
                continue
            if not isinstance(m["sha256"], str) or HEX64.fullmatch(m["sha256"]) is None:
                problems.append(f"{mtag}: sha256 not 64 lowercase hex")
            if not _is_int(m["size"]) or m["size"] < 0:
                problems.append(f"{mtag}: size must be a non-negative integer (not bool)")
            elif m["size"] > MAX_MEMBER_BYTES:
                problems.append(f"{mtag}: expected size {m['size']} exceeds bound "
                                f"{MAX_MEMBER_BYTES} (pre-IO)")
            else:
                total_expected += m["size"]
                if total_expected > MAX_AGGREGATE_BYTES:
                    problems.append(f"aggregate expected size exceeds bound "
                                    f"{MAX_AGGREGATE_BYTES} (pre-IO)")
            for field in ("member", "target"):
                try:
                    validate_relative_path(m[field])
                except VerifyError as e:
                    problems.append(f"{mtag}: bad {field} path: {e}")
            mcf = m["member"].casefold()
            if mcf in seen_members:
                problems.append(f"{mtag}: duplicate/casefold-aliased member "
                                f"{m['member']!r} (also at {seen_members[mcf]})")
            else:
                seen_members[mcf] = mtag
            if mcf in seen_dirs:
                problems.append(f"{mtag}: member {m['member']!r} collides with "
                                f"directory entry (casefolded)")
            key = m["target"].casefold()
            if key in seen_targets:
                problems.append(f"{mtag}: duplicate/casefold-aliased target "
                                f"{m['target']!r} (also at {seen_targets[key]})")
            else:
                seen_targets[key] = mtag
            if key in seen_dirs:
                problems.append(f"{mtag}: target {m['target']!r} collides with "
                                f"directory entry (casefolded)")
            total_members_all += 1
        if not isinstance(w.get("fully_covered"), bool) or w["fully_covered"] != (not w["not_covered"]):
            problems.append(f"{tag}: fully_covered must be a consistent boolean")
        for m in w["members"]:
            if isinstance(m, dict) and isinstance(m.get("member"), str):
                target, reason = map_member(m["member"])
                if target != m.get("target") or reason is not None:
                    problems.append(f"{tag}: covered member mapping contradiction")
        for j, nc in enumerate(w["not_covered"]):
            ntag = f"{tag}.not_covered[{j}]"
            if not isinstance(nc, dict) or set(nc) != {"member", "reason"}:
                problems.append(f"{ntag}: unexpected fields")
                continue
            if not isinstance(nc["member"], str) or not isinstance(nc["reason"], str):
                problems.append(f"{ntag}: member/reason must be strings")
                continue
            try:
                validate_relative_path(nc["member"])
            except VerifyError as e:
                problems.append(f"{ntag}: bad not-covered member path: {e}")
            cf = nc["member"].casefold()
            if cf in seen_members or cf in seen_dirs:
                problems.append(f"{ntag}: duplicate/aliased not-covered member")
            seen_members[cf] = ntag
            target, reason = map_member(nc["member"])
            if target is not None or reason is None or not nc["reason"]:
                problems.append(f"{ntag}: not-covered mapping contradiction")
    if isinstance(wheels, list) and not problems:
        if total_members_all == 0:
            problems.append("projection covers zero files across all wheels; "
                            "a pass would verify nothing")
    return problems


def compute_projection_hash(doc):
    """Recompute the projection content hash from the manifest. Consistency
    only: this proves the manifest is internally intact, not that it is
    authentic (no signature exists)."""
    wheels = []
    for w in doc.get("wheels", []):
        wheels.append({k: w[k] for k in
                       ("filename", "name", "version", "archive_sha256",
                        "members", "not_covered", "directories",
                        "fully_covered") if k in w})
    core = {"schema": doc.get("schema"), "version": doc.get("version"),
            "wheels": wheels}
    blob = json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def resolve_root(root_arg):
    """The explicit root: must exist, be a real directory and not a symlink.
    Returns (resolved_root, problems)."""
    problems = []
    if not isinstance(root_arg, str) or not root_arg:
        return None, ["site-packages root argument missing"]
    if not os.path.exists(root_arg):
        return None, [f"site-packages root does not exist: {root_arg!r}"]
    if os.path.islink(root_arg):
        return None, [f"site-packages root is a symlink: {root_arg!r}"]
    resolved = os.path.realpath(root_arg)
    if not os.path.isdir(resolved):
        return None, [f"site-packages root is not a directory: {root_arg!r}"]
    return resolved, problems


def check_file(resolved_root, target):
    """One projected file. Returns a problem string or None. Reads the file's
    bytes only after every path/ancestor/leaf check passes. Never writes."""
    try:
        comps = validate_relative_path(target)
    except VerifyError as e:
        return f"target {target!r}: path schema refused: {e}"
    cur = resolved_root
    for comp in comps[:-1]:
        cur = os.path.join(cur, comp)
        if not os.path.lexists(cur):
            return f"target {target!r}: missing ancestor directory {comp!r}"
        if os.path.islink(cur):
            return f"target {target!r}: symlink ancestor {comp!r} refused"
        if not os.path.isdir(cur):
            return f"target {target!r}: ancestor {comp!r} is not a directory"
    leaf = os.path.join(cur, comps[-1])
    if not os.path.lexists(leaf):
        return f"target {target!r}: missing projected file"
    if os.path.islink(leaf):
        return f"target {target!r}: leaf is a symlink, refused"
    st = os.lstat(leaf)
    if not stat.S_ISREG(st.st_mode):
        return f"target {target!r}: leaf is not a regular file"
    real = os.path.realpath(leaf)
    try:
        if os.path.commonpath([real, resolved_root]) != resolved_root:
            return f"target {target!r}: resolved path escapes the site-packages root"
    except ValueError:
        return f"target {target!r}: resolved path escapes the site-packages root"
    return None


def hash_file(resolved_root, target, expected_size, expected_sha):
    comps = target.split("/")
    leaf = os.path.join(resolved_root, *comps)
    # Stat-size gate BEFORE any hashing: never stream an unbounded file when
    # its on-disk size already contradicts the projection.
    try:
        st = os.lstat(leaf)
    except OSError as e:
        return f"target {target!r}: unreadable: {e.__class__.__name__}: {e}"
    if not stat.S_ISREG(st.st_mode):
        return f"target {target!r}: leaf is not a regular file"
    if st.st_size != expected_size:
        return (f"target {target!r}: size mismatch (on-disk {st.st_size} != "
                f"projected {expected_size}; refused before hashing)")
    h = hashlib.sha256()
    count = 0
    try:
        with open(leaf, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
                count += len(chunk)
                if count > expected_size:  # grew between stat and read
                    return (f"target {target!r}: size mismatch (grew past "
                            f"projected {expected_size} while reading)")
    except OSError as e:
        return f"target {target!r}: unreadable: {e.__class__.__name__}: {e}"
    if count != expected_size:
        return (f"target {target!r}: size mismatch (actual {count} != projected "
                f"{expected_size})")
    if h.hexdigest() != expected_sha:
        return f"target {target!r}: sha256 mismatch (bytes changed since projection)"
    return None


def verify_projection(doc, root_arg):
    """Verify the installed projected payload. Returns the report dict."""
    problems = validate_manifest(doc)
    resolved_root, root_problems = resolve_root(root_arg)
    problems += root_problems
    files_verified = 0
    wheels_verified = 0
    not_covered = []
    if not problems:
        if compute_projection_hash(doc) != doc.get("projection_sha256"):
            problems.append("projection hash inconsistency: manifest content does "
                            "not match its projection_sha256 (damaged or tampered "
                            "manifest); NOT proof of malice, but refusing")
    if not problems:
        # Path/ancestor/leaf phase for ALL targets before any payload read.
        pending = []
        for w in doc["wheels"]:
            for m in w["members"]:
                pending.append((w["filename"], m))
        for filename, m in pending:
            p = check_file(resolved_root, m["target"])
            if p is not None:
                problems.append(f"{filename}: {p}")
        # Payload phase.
        if not problems:
            done = set()
            for w in doc["wheels"]:
                wheel_ok = True
                for m in w["members"]:
                    p = hash_file(resolved_root, m["target"], m["size"], m["sha256"])
                    if p is not None:
                        problems.append(f"{w['filename']}: {p}")
                        wheel_ok = False
                    else:
                        files_verified += 1
                if wheel_ok and w["members"]:
                    done.add(w["filename"])
            wheels_verified = len(done)
            if problems:
                files_verified = 0
                wheels_verified = 0
    if isinstance(doc, dict) and isinstance(doc.get("wheels"), list):
        for w in doc["wheels"]:
            if isinstance(w, dict) and isinstance(w.get("not_covered"), list):
                for nc in w["not_covered"]:
                    if isinstance(nc, dict):
                        not_covered.append({"wheel": w.get("filename"),
                                            "member": nc.get("member"),
                                            "reason": nc.get("reason")})
    return {"format": RESULT_FORMAT, "version": 1,
            "status": "refused" if problems else "pass",
            "problems": problems,
            "checked": list(CHECKED),
            "not_checkable": list(NOT_CHECKABLE),
            "not_covered": not_covered,
            "files_verified": files_verified,
            "wheels_verified": wheels_verified,
            "root": resolved_root if resolved_root else None,
            "non_operational": True, "note": NOTE,
            "scope_limit": SCOPE_LIMIT,
            "path_identity_limit": PATH_IDENTITY_LIMIT,
            "unverified": "UNVERIFIED against the real repo; hypothesis until "
                          "checked natively on Windows"}


def _early_refusal(problems):
    return {"format": RESULT_FORMAT, "version": 1, "status": "refused",
            "problems": list(problems), "checked": [], "files_verified": 0,
            "wheels_verified": 0, "not_covered": [], "root": None,
            "not_checkable": list(NOT_CHECKABLE),
            "non_operational": True, "note": NOTE, "scope_limit": SCOPE_LIMIT,
            "path_identity_limit": PATH_IDENTITY_LIMIT}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 2:
        print(json.dumps(_early_refusal(
            ["usage: verify_wheel_projection.py PROJECTION_JSON "
             "SITE_PACKAGES_ROOT"]), indent=2, sort_keys=True))
        return 2
    try:
        with open(argv[0], "r", encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError) as e:
        print(json.dumps(_early_refusal(
            [f"projection unreadable: {e.__class__.__name__}: {e}"]), indent=2,
            sort_keys=True))
        return 2
    report = verify_projection(doc, argv[1])
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    sys.exit(main())
