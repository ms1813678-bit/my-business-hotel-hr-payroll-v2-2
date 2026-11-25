"""NEW stdlib-only wheel-payload projection builder (NOT a recovered format).

UNVERIFIED against the real repo. Hypothesis until checked against the real
repository and run natively on Windows. Synthetic fixtures only in its tests.

What it does (0404 brief):

  * Input: an existing strict NEW "hony-wheels-lock" v1 lock document plus the
    exact wheelhouse directory it describes. The lock structure, strict wheel
    file names, name/version agreement and the archive sha256 of every wheel
    are independently enforced here, and every wheel's archive bytes are
    re-hashed and compared against the lock BEFORE any zip inspection.
  * Output: a NEW "hony-wheel-projection" v1 manifest (JSON) listing, per
    wheel, the archive origin sha256, every covered original member with its
    intended RELATIVE site-packages target path and its computed sha256/size,
    and every NOT_COVERED member with a reason. `projection_sha256` is a hash
    over the projection content; it is NOT an archive sha.
  * No zip extraction: members are read as byte streams for hashing only;
    nothing is written to any site-packages tree, nothing is imported or
    executed (.pth and PE payloads are hashed as inert bytes only).

Mapping boundary (first implementation, per 0404 brief):

  * Ordinary wheel-root members are byte-preserved site-packages payload,
    including .dist-info metadata EXCEPT RECORD (rewritten at install).
  * Only `<name>-<version>.data/purelib` and `.data/platlib` relocation is
    supported, with exact mapping and collision detection.
  * `.data/scripts`, `.data/headers`, `.data/data` and any other `.data`
    scheme are reported NOT_COVERED (transformed at install; not verified).
    The actual 50 wheels include xlrd/bottle/pywin32 `.data/scripts`; those
    are NOT silently dropped and NO whole-distribution or flat-copy
    whole-install integrity is ever claimed.
  * RECORD, installed RECORD, bytecode (*.pyc), console entrypoint wrappers,
    pip INSTALLER/REQUESTED/direct_url.json and runtime-generated files are
    NOT claimed covered; if present as wheel members they are reported
    NOT_COVERED, never mapped.

Refusals (build fails, exit 2): absolute/backslash/drive/ADS/traversal/
NUL/control/trailing-dot-space/device/empty/dot components in member names;
a raw archive name containing NUL (ZipInfo.orig_filename checked BEFORE the
truncated filename or any payload read; CPython zipfile truncates names at
NUL, which would otherwise alias a hostile name to a safe target); duplicate
zip member names; casefold aliases within a wheel; duplicate mapped targets
even across wheels (casefolded); symlinks and other non-regular special
entries; directory entries that are non-empty, mistyped, or collide with a
member or mapped target; unsupported compression (only STORED and DEFLATED
are supported) or encryption; unreasonable declared or actual byte counts or
expansion ratio; member-count and aggregate-size bounds exceeded
(cumulative across wheels, enforced before payload reads).

Entry-kind policy (0413 repair): a Unix mode with no file-type bits
(kind 0, permissions only, e.g. an annotated-doc RECORD archived 0600) is an
ordinary regular member. Explicit empty directory entries (trailing slash,
declared size 0, actual size 0, consistent typing: S_IFDIR or untyped) are
accepted and RECORDED under "directories" for accounting only; they are
never payload targets. Symlinks, fifos, devices and other special entries
are still refused.

Security-sensitive checks (raw names, counts, declared sizes, entry kinds,
flags) all precede any payload reads, and cross-wheel count/size bounds are
enforced cumulatively in the structural pass. Computed bytes
(sha256/size/CRC via zipfile) are recorded, never just central-directory
claims.

CLI: python build_wheel_projection.py WHEELS_LOCK_JSON WHEELHOUSE_DIR [OUT_JSON]
Exit 0 and the manifest (file or stdout) on success; exit 2 and one JSON
refusal report on stdout otherwise. Reads only; writes only the optional OUT
file; no network, no installs, no environment mutations.
"""
import hashlib
import json
import os
import re
import stat
import sys
import zipfile

PROJECTION_FORMAT = "hony-wheel-projection"   # NEW schema, NOT a recovered format
PROJECTION_VERSION = 1
BUILD_REPORT_FORMAT = "hony-wheel-projection-build/v1"
LOCK_FORMAT = "hony-wheels-lock"              # existing NEW v1 lock (read-only input)
LOCK_VERSION = 1

NOTE = ("Read-only projection build. Extracts nothing, installs nothing, imports "
        "no application code, touches no network, config, database or environment.")
SCOPE_LIMIT = ("Original wheel payload projection integrity only. This is NOT "
               "whole-distribution integrity, NOT an exact full installed-tree "
               "check, and NOT a malware or runtime security approval.")
PATH_IDENTITY_LIMIT = ("Lexical path checks only. Windows junction/reparse-point/"
                       "8.3-short-name identity and filesystem races are NOT proved "
                       "by these checks; native path identity is a writer/native "
                       "question.")

# Explicit bounds (recorded into every manifest). Overridable for tests only.
DEFAULT_LIMITS = {
    "max_members_per_wheel": 20000,
    "max_total_members": 200000,
    "max_member_bytes": 256 * 1024 * 1024,        # 256 MiB per member (declared+actual)
    "max_aggregate_bytes": 4 * 1024 * 1024 * 1024,  # 4 GiB total uncompressed (declared+actual)
    "max_compression_ratio": 100,                  # declared/actual expansion bound
}

HEX64 = re.compile(r"^[0-9a-f]{64}$")
_WHEEL_PART = r"[A-Za-z0-9][A-Za-z0-9_.+!]*"
WHEEL_FILE_RE = re.compile(r"^(%s)-(%s)(?:-(\d[A-Za-z0-9_.]*))?-(%s)-(%s)-(%s)\.whl$"
                           % ((_WHEEL_PART,) * 5))
DEVICE_NAMES = ({"CON", "PRN", "AUX", "NUL"}
                | {f"COM{i}" for i in range(1, 10)}
                | {f"LPT{i}" for i in range(1, 10)})
SUPPORTED_COMPRESSION = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)

# pip-generated dist-info files: install-time artifacts, never original payload.
PIP_GENERATED_DIST_INFO = {"INSTALLER", "REQUESTED", "direct_url.json"}


class ProjectionError(Exception):
    """Refusal. .problems carries the human-readable refusal reasons."""

    def __init__(self, problems):
        if isinstance(problems, str):
            problems = [problems]
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def wheel_pin(filename):
    """Strict bare wheel file name; pure string check, no IO."""
    if not isinstance(filename, str) or len(filename) > 255:
        raise ProjectionError("not a wheel file name: bad type or length")
    m = WHEEL_FILE_RE.fullmatch(filename)
    if not m:
        raise ProjectionError(f"not a wheel file name (strict bare name required): {filename!r}")
    return canonical(m.group(1)), m.group(2)


def check_lock_structure(doc):
    """Structural checks on the existing NEW hony-wheels-lock v1 document."""
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
        except ProjectionError as e:
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


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def check_wheelhouse(lock_doc, wheelhouse_dir):
    """The wheelhouse must contain exactly the locked wheel files (no missing
    locked wheel, no extra *.whl). Names only here; bytes are hashed by the caller
    before any zip inspection."""
    problems = []
    if not isinstance(wheelhouse_dir, str) or not wheelhouse_dir:
        return ["wheelhouse directory argument missing"]
    if not os.path.isdir(wheelhouse_dir):
        return [f"wheelhouse directory does not exist: {wheelhouse_dir!r}"]
    locked = {w["filename"]: w for w in lock_doc["wheels"]}
    try:
        present = sorted(os.listdir(wheelhouse_dir))
    except OSError as e:
        return [f"wheelhouse unreadable: {e.__class__.__name__}: {e}"]
    wheels_present = [f for f in present if f.lower().endswith(".whl")]
    for fn in wheels_present:
        if fn not in locked:
            problems.append(f"wheelhouse contains wheel not in the lock: {fn!r}")
    for fn in locked:
        if fn not in wheels_present:
            problems.append(f"locked wheel missing from wheelhouse: {fn!r}")
            continue
        p = os.path.join(wheelhouse_dir, fn)
        if os.path.islink(p):
            problems.append(f"wheel is a symlink: {fn!r}")
        elif not os.path.isfile(p):
            problems.append(f"wheel is not a regular file: {fn!r}")
    return problems


# ------------------------------------------------------------- member path policy
def validate_relative_path(name, what="member name"):
    """NEW strict relative POSIX-path policy for zip member names and projected
    target paths (same rules both sides). Lexical only (PATH_IDENTITY_LIMIT).
    Returns the component list; raises ProjectionError on refusal."""
    if not isinstance(name, str) or not name:
        raise ProjectionError(f"{what}: empty or not a string")
    if len(name) > 512:
        raise ProjectionError(f"{what}: overlong path ({len(name)} chars)")
    if "\x00" in name:
        raise ProjectionError(f"{what}: NUL character")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ProjectionError(f"{what}: control character")
    if "\\" in name:
        raise ProjectionError(f"{what}: backslash separator")
    if name.startswith("/"):
        raise ProjectionError(f"{what}: absolute path")
    if ":" in name:
        raise ProjectionError(f"{what}: colon (drive letter or alternate data stream)")
    comps = name.split("/")
    for c in comps:
        if not c:
            raise ProjectionError(f"{what}: empty component")
        if c == ".":
            raise ProjectionError(f"{what}: dot component")
        if c == "..":
            raise ProjectionError(f"{what}: parent-directory traversal")
        if c.endswith(".") or c.endswith(" "):
            raise ProjectionError(f"{what}: trailing dot or space in component {c!r}")
        stem = c.split(".", 1)[0].upper()
        if stem in DEVICE_NAMES:
            raise ProjectionError(f"{what}: reserved device name {c!r}")
    return comps


def _casefold_key(rel):
    return rel.casefold()


# ------------------------------------------------------------------ zip handling
def _entry_kind(info):
    """Classify a zip entry. Returns "file" or "directory"; raises
    ProjectionError for symlinks and other non-regular special entries.

    0413 repair: a Unix mode whose file-type bits are zero (kind 0,
    permission bits only, e.g. a RECORD archived 0600) is an ordinary
    regular member, not a special entry. Explicit EMPTY directory entries
    (trailing slash, consistent typing) are accepted as "directory" and
    handled by the caller; they are never payload targets."""
    is_dir_name = info.filename.endswith("/")
    if info.create_system == 3:  # unix: mode bits are meaningful
        mode = (info.external_attr >> 16) & 0xFFFF
        kind = stat.S_IFMT(mode) if mode else 0
        if kind == stat.S_IFLNK:
            raise ProjectionError(f"symlink entry refused: {info.filename!r}")
        if kind == stat.S_IFDIR:
            if not is_dir_name:
                raise ProjectionError(
                    f"directory-typed entry without a directory name refused: "
                    f"{info.filename!r} (mode {oct(mode)})")
            return "directory"
        if kind not in (stat.S_IFREG, 0):
            raise ProjectionError(
                f"special (non-regular) entry refused: {info.filename!r} "
                f"(mode {oct(mode)})")
        if is_dir_name:
            if kind == stat.S_IFREG:
                raise ProjectionError(
                    f"directory entry {info.filename!r} typed as a regular "
                    f"file (inconsistent entry type)")
            return "directory"  # kind 0 + trailing slash: explicit, untyped
        return "file"
    # Non-unix archives carry no mode bits; the trailing slash is the only
    # explicit type marker a directory entry has.
    return "directory" if is_dir_name else "file"


def inspect_wheel(path, filename, limits, budget=None):
    """Read one wheel. Returns (members, not_covered, directories):
      members:     [{"member","target","sha256","size"}] computed bytes only
      not_covered: [{"member","reason"}]
      directories: [names] explicit empty directory entries (never targets)
    budget: cumulative cross-wheel accounting {"members","declared","actual"},
    mutated in place; every bound is enforced BEFORE payload reads where the
    information exists without reading payload (counts, declared sizes).
    All other security-sensitive checks precede every payload read."""
    if budget is None:
        budget = {"members": 0, "declared": 0, "actual": 0}
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError, EOFError) as e:
        raise ProjectionError(f"{filename}: unreadable archive: "
                              f"{e.__class__.__name__}: {e}")
    with zf:
        try:
            infos = zf.infolist()
        except (zipfile.BadZipFile, EOFError) as e:
            raise ProjectionError(f"{filename}: unreadable central directory: "
                                  f"{e.__class__.__name__}: {e}")
        # Member-count bounds: pre-IO, cumulative across wheels.
        if len(infos) > limits["max_members_per_wheel"]:
            raise ProjectionError(
                f"{filename}: member count {len(infos)} exceeds bound "
                f"{limits['max_members_per_wheel']}")
        if budget["members"] + len(infos) > limits["max_total_members"]:
            raise ProjectionError(
                f"total member count exceeds bound {limits['max_total_members']} "
                f"(cumulative across wheels; checked before any payload read)")

        # ---- structural pass, no payload reads ----
        seen_names = set()
        seen_casefold = {}
        seen_dirs = {}
        dir_infos = set()
        directories = []
        for info in infos:
            # Raw archive name FIRST: CPython zipfile truncates a member name
            # at NUL, which would alias a hostile raw name to a safe-looking
            # target. Refuse before the truncated filename is used or read.
            orig = getattr(info, "orig_filename", info.filename)
            if "\x00" in orig:
                raise ProjectionError(
                    f"{filename}: raw archive member name contains NUL "
                    f"(truncated by zipfile to {info.filename!r}); refused")
            name = info.filename
            if name in seen_names:
                raise ProjectionError(f"{filename}: duplicate zip member name {name!r}")
            seen_names.add(name)
            kind = _entry_kind(info)  # symlink/special/mistyped refused first
            if kind == "directory":
                # Explicit empty directory entry: safe path, declared empty,
                # unique, colliding with nothing. Accounting only.
                dname = name[:-1]
                validate_relative_path(dname, what=f"{filename}: directory name")
                if not isinstance(info.file_size, int) or info.file_size != 0:
                    raise ProjectionError(
                        f"{filename}: directory entry {name!r} has nonzero "
                        f"declared size {info.file_size!r}; not an empty "
                        f"directory entry")
                dcf = _casefold_key(dname)
                if dcf in seen_dirs:
                    raise ProjectionError(
                        f"{filename}: duplicate/casefold-aliased directory "
                        f"entries {seen_dirs[dcf]!r} and {name!r}")
                if dcf in seen_casefold:
                    raise ProjectionError(
                        f"{filename}: directory entry {name!r} collides with "
                        f"member {seen_casefold[dcf]!r} (casefolded)")
                seen_dirs[dcf] = name
                dir_infos.add(id(info))
                directories.append(name)
                budget["members"] += 1
                continue
            cf = _casefold_key(name)
            if cf in seen_casefold:
                raise ProjectionError(
                    f"{filename}: casefold alias members {seen_casefold[cf]!r} and {name!r}")
            if cf in seen_dirs:
                raise ProjectionError(
                    f"{filename}: member {name!r} collides with directory "
                    f"entry {seen_dirs[cf]!r} (casefolded)")
            seen_casefold[cf] = name
            validate_relative_path(name, what=f"{filename}: member name")
            if info.flag_bits & 0x1:
                raise ProjectionError(f"{filename}: encrypted entry refused: {name!r}")
            if info.compress_type not in SUPPORTED_COMPRESSION:
                raise ProjectionError(
                    f"{filename}: unsupported compression {info.compress_type} for {name!r}")
            if not isinstance(info.file_size, int) or info.file_size < 0:
                raise ProjectionError(f"{filename}: bad declared size for {name!r}")
            if info.file_size > limits["max_member_bytes"]:
                raise ProjectionError(
                    f"{filename}: declared size {info.file_size} of {name!r} exceeds "
                    f"bound {limits['max_member_bytes']}")
            # Declared aggregate bound: pre-IO, cumulative across wheels.
            budget["declared"] += info.file_size
            if budget["declared"] > limits["max_aggregate_bytes"]:
                raise ProjectionError(
                    f"aggregate declared size exceeds bound "
                    f"{limits['max_aggregate_bytes']} (cumulative across wheels; "
                    f"checked before any payload read; last member {filename}:{name})")
            if info.compress_type == zipfile.ZIP_DEFLATED and info.file_size > 0:
                if info.file_size > max(info.compress_size, 1) * limits["max_compression_ratio"]:
                    raise ProjectionError(
                        f"{filename}: expansion ratio of {name!r} exceeds bound "
                        f"{limits['max_compression_ratio']} "
                        f"({info.file_size} declared from {info.compress_size} compressed)")
            budget["members"] += 1

        # ---- payload pass: compute bytes, verify against declared metadata ----
        members = []
        not_covered = []
        for info in infos:
            name = info.filename
            if id(info) in dir_infos:
                # Directory entries must be actually empty, not just declared.
                try:
                    with zf.open(info, "r") as f:
                        extra = f.read(1)
                except Exception as e:
                    raise ProjectionError(
                        f"{filename}: directory entry {name!r} unreadable: "
                        f"{e.__class__.__name__}: {e}")
                if extra:
                    raise ProjectionError(
                        f"{filename}: directory entry {name!r} has payload "
                        f"bytes; not an empty directory entry")
                continue
            h = hashlib.sha256()
            count = 0
            try:
                with zf.open(info, "r") as f:
                    for chunk in iter(lambda: f.read(1024 * 1024), b""):
                        h.update(chunk)
                        count += len(chunk)
                        if count > limits["max_member_bytes"]:
                            raise ProjectionError(
                                f"{filename}: actual size of {name!r} exceeds bound "
                                f"{limits['max_member_bytes']}")
                        # Actual aggregate bound: cumulative across wheels.
                        budget["actual"] += len(chunk)
                        if budget["actual"] > limits["max_aggregate_bytes"]:
                            raise ProjectionError(
                                f"aggregate actual size exceeds bound "
                                f"{limits['max_aggregate_bytes']} (cumulative "
                                f"across wheels)")
            except ProjectionError:
                raise
            except Exception as e:  # BadZipFile, EOFError, OSError, zlib.error, ...
                # Any payload-read failure is a refusal, never a silent skip.
                raise ProjectionError(
                    f"{filename}: member {name!r} bytes failed verification "
                    f"(CRC/length/integrity): {e.__class__.__name__}: {e}")
            if count != info.file_size:
                raise ProjectionError(
                    f"{filename}: member {name!r} actual size {count} != declared "
                    f"{info.file_size}")
            target, reason = map_member(name)
            if target is not None:
                tcf = _casefold_key(target)
                if tcf in seen_dirs:
                    raise ProjectionError(
                        f"{filename}: mapped target {target!r} of member {name!r} "
                        f"collides with directory entry {seen_dirs[tcf]!r} "
                        f"(casefolded)")
                members.append({"member": name, "target": target,
                                "sha256": h.hexdigest(), "size": count})
            else:
                not_covered.append({"member": name, "reason": reason})
        return members, not_covered, directories


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
        if base in PIP_GENERATED_DIST_INFO:
            return None, (f"{base} is generated by the installer, not original "
                          f"wheel payload; not verified")
        return name, None
    if name.endswith(".pyc"):
        return None, "bytecode is runtime-generated; not claimed covered"
    return name, None


def compute_projection_hash(core):
    """Hash over the projection CONTENT (schema/version/wheels with members,
    targets, hashes and not-covered lists). This is NOT any wheel archive sha."""
    blob = json.dumps(core, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def projection_core(wheels_out):
    return {"schema": PROJECTION_FORMAT, "version": PROJECTION_VERSION,
            "wheels": wheels_out}


def build_projection(lock_doc, wheelhouse_dir, limits=None):
    """Build the NEW projection manifest from a lock document (already parsed)
    and a wheelhouse directory. Raises ProjectionError on any refusal."""
    limits = dict(DEFAULT_LIMITS if limits is None else limits)
    problems = check_lock_structure(lock_doc)
    if problems:
        raise ProjectionError(problems)
    problems = check_wheelhouse(lock_doc, wheelhouse_dir)
    if problems:
        raise ProjectionError(problems)

    # Archive sha256 against the lock BEFORE any zip inspection.
    wheels_sorted = sorted(lock_doc["wheels"], key=lambda w: w["filename"])
    archive_shas = {}
    for w in wheels_sorted:
        p = os.path.join(wheelhouse_dir, w["filename"])
        try:
            actual = sha256_file(p)
        except OSError as e:
            raise ProjectionError(f"{w['filename']}: unreadable wheel file: "
                                  f"{e.__class__.__name__}: {e}")
        if actual != w["sha256"]:
            raise ProjectionError(
                f"{w['filename']}: archive sha256 mismatch (computed {actual} != "
                f"locked {w['sha256']}); zip NOT inspected")
        archive_shas[w["filename"]] = actual

    wheels_out = []
    targets = {}  # casefolded target -> (wheel filename, member)
    budget = {"members": 0, "declared": 0, "actual": 0}  # cross-wheel, pre-IO
    for w in wheels_sorted:
        p = os.path.join(wheelhouse_dir, w["filename"])
        members, not_covered, directories = inspect_wheel(
            p, w["filename"], limits, budget)
        for m in members:
            key = _casefold_key(m["target"])
            if key in targets:
                other_wheel, other_member = targets[key]
                raise ProjectionError(
                    f"duplicate mapped target {m['target']!r}: {w['filename']}"
                    f":{m['member']} collides with {other_wheel}:{other_member} "
                    f"(casefolded target collision, cross-wheel targets must be unique)")
            targets[key] = (w["filename"], m["member"])
        wheels_out.append({
            "filename": w["filename"],
            "name": w["name"],
            "version": w["version"],
            "archive_sha256": archive_shas[w["filename"]],
            "members": members,
            "not_covered": not_covered,
            "directories": directories,  # explicit empty dirs; never targets
            "fully_covered": not not_covered,
        })

    core = projection_core(wheels_out)
    manifest = dict(core)
    manifest.update({
        "status": "NEW - not a recovered format",
        "generator": "build_wheel_projection.py (stdlib-only, NEW)",
        "non_operational": True,
        "note": NOTE,
        "scope_limit": SCOPE_LIMIT,
        "path_identity_limit": PATH_IDENTITY_LIMIT,
        "whole_install_integrity_claimed": False,
        "projection_sha256": compute_projection_hash(core),
        "projection_hash_note": "sha256 over the projection content; NOT a wheel archive sha",
        "source_lock": {"schema": LOCK_FORMAT, "version": LOCK_VERSION},
        "limits": limits,
        "unverified": "UNVERIFIED against the real repo; hypothesis until checked "
                      "natively on Windows",
    })
    return manifest


def _refusal_report(problems):
    return {"format": BUILD_REPORT_FORMAT, "version": 1, "status": "refused",
            "problems": list(problems), "non_operational": True, "note": NOTE,
            "scope_limit": SCOPE_LIMIT}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) not in (2, 3):
        print(json.dumps(_refusal_report(
            ["usage: build_wheel_projection.py WHEELS_LOCK_JSON WHEELHOUSE_DIR "
             "[OUT_JSON]"]), indent=2, sort_keys=True))
        return 2
    lock_path, wheelhouse_dir = argv[0], argv[1]
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            lock_doc = json.load(f)
    except (OSError, ValueError) as e:
        print(json.dumps(_refusal_report(
            [f"lock unreadable: {e.__class__.__name__}: {e}"]), indent=2, sort_keys=True))
        return 2
    try:
        manifest = build_projection(lock_doc, wheelhouse_dir)
    except ProjectionError as e:
        print(json.dumps(_refusal_report(e.problems), indent=2, sort_keys=True))
        return 2
    out = json.dumps(manifest, indent=2, sort_keys=True)
    if len(argv) == 3:
        try:
            with open(argv[2], "w", encoding="utf-8") as f:
                f.write(out + "\n")
        except OSError as e:
            print(json.dumps(_refusal_report(
                [f"output unwritable: {e.__class__.__name__}: {e}"]), indent=2,
                sort_keys=True))
            return 2
    else:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
