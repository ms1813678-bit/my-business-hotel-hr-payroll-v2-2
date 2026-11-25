"""NEW stage-tree / verify-tree CLI for the offline source-stage tree (Bv3 next interface).

UNVERIFIED against the real repo. Synthetic data only, Linux. Hypotheses until checked
against the real repository and on Windows. One entry module, two commands:

    stage-tree  --source-root DIR --source-manifest FILE --wheelhouse DIR
                --requirements FILE --runtime-inventory FILE --inputs DIR
                --destination NEWDIR --version VALUE
    verify-tree --root DIR

What this does and does not do (contract from the 0335 interface brief):

  * Creates ONLY a new stage tree. The destination must not exist beforehand: no
    overwrite, no reuse, no merge. On any refusal the destination is left exactly as
    far as it got (NO cleanup deletion on failure) and is marked INCOMPLETE via
    stage-state.json so verify-tree refuses it; a human must inspect it.
  * Copies ONLY what the NEW explicit source manifest lists (relative path + sha256
    per entry). It never recursive-copies unknown checkout contents. It refuses
    absolute paths, traversal, backslashes/drive forms, symlinks, special files,
    case-collisions, any actual .env, runtime var/data/backup trees, VCS/cache/venv
    components, private keys/certs (by name and by PEM content marker), and paths
    outside the allowed app-source roots. `.env.example` is the sole env-file
    exception and must contain only public placeholder values (conservative NEW
    policy, fails closed; see check_env_example).
  * Every copied byte is hashed while copying and re-hashed after writing; the tree
    is then verified exactly (no unexpected files) before the completion marker is
    written. There is NO claim of whole-system atomicity: the completion marker is a
    last-written marker, not a transaction.
  * Wheelhouse: exact hashed input via the existing strict helpers
    (parse_hashed_requirements / verify_hashed_wheelhouse / build_wheels_lock) PLUS a
    complete strict tree check: the wheelhouse directory must contain exactly the
    listed .whl files and nothing else (no subdirectories, no unlisted artifacts).
    Real wheel metadata/tags still need the writer's native acceptance separately.
  * Runtime inventory must carry pinned sha256, a safe bare basename, reviewed
    non-empty silent_args and ok_exit_codes == [0] exactly (the recovered, unchanged
    ISS accepts only exit 0). Missing real inputs REFUSE; nothing invents flags,
    downloads or exit codes.
  * No implicit network access, installer execution or subprocess execution. Nothing
    here runs anything.

Stage layout (from the consumer contract; the recovered ISS takes /DStageRoot=<dest>\\inno):

    <dest>/inno/app-src/{app,alembic,scripts,docs}/... plus pyproject.toml, alembic.ini
          and optional .env.example, README.md, USER_GUIDE.md
    <dest>/inno/wheelhouse/*.whl                       (exactly the locked wheels)
    <dest>/inno/requirements-locked.txt                (the hashed requirements, byte copy)
    <dest>/inno/inputs/<safe macro basenames>          (exactly the inventory files)
    <dest>/inno/installer-checks/{wheels.lock.json, verify_install.py, install-plan.json,
                                  dependency-inventory.json, ownership-state.template.json}
    <dest>/inno/staged-inputs.iss.inc                  (NEW generator, recovered macro names)
    <dest>/stage-manifest.json                         (NEW hony-stage-manifest v1)
    <dest>/stage-state.json                            (NEW hony-stage-state v1: incomplete->complete)

Output is machine-readable JSON with format/version, status, refusal_reasons and
non_operational=true. Exit 0 on success, exit 2 on any missing/malformed/mismatched/
unsafe input. Lexical path policy only: it cannot disprove junction/reparse/8.3
identity (unresolved, same limit as hony_journal.PATH_IDENTITY_LIMIT).
"""
import argparse
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path

# hony_stage is a sibling module (no package context); load it by path.
_SPEC = importlib.util.spec_from_file_location(
    "hony_stage", Path(__file__).resolve().parent / "hony_stage.py")
hs = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(hs)

StageError = hs.StageError

SRCMAN_FORMAT = "hony-source-manifest"          # NEW schema, NOT recovered
SRCMAN_VERSION = 1
INVENTORY_FORMAT = "hony-runtime-inventory"     # NEW schema, NOT recovered
INVENTORY_VERSION = 1
STAGE_MAN_FORMAT = "hony-stage-manifest"        # NEW schema, NOT recovered
STAGE_MAN_VERSION = 1
STATE_FORMAT = "hony-stage-state"               # NEW schema, NOT recovered
STATE_VERSION = 1
STAGE_RESULT_FORMAT = "hony-stage-tree-result/v1"
VERIFY_RESULT_FORMAT = "hony-verify-tree-result/v1"

STATE_FILE = "stage-state.json"
MANIFEST_FILE = "stage-manifest.json"
INNO_DIR = "inno"
TOP_LEVEL = {INNO_DIR, STATE_FILE, MANIFEST_FILE}

# The recovered ISS consumes these app-src members; the four directories and the two
# config files are mandatory in the recovered [Files] section (no skipifsourcedoesntexist),
# the three doc/env members are optional there (skipifsourcedoesntexist).
ALLOWED_DIR_ROOTS = ("app", "alembic", "scripts", "docs")
REQUIRED_DIR_ROOTS = ("app", "alembic", "scripts", "docs")  # Inno errors on an empty Source
ALLOWED_FILE_ROOTS = ("pyproject.toml", "alembic.ini", ".env.example", "README.md", "USER_GUIDE.md")
REQUIRED_FILE_ROOTS = ("pyproject.toml", "alembic.ini")

VCS_CACHE_VENV = {".git", ".hg", ".svn", "__pycache__", ".cache", ".pytest_cache",
                  ".mypy_cache", ".ruff_cache", ".venv", "venv"}
RUNTIME_TREE_NAMES = {"var", "data", "backup", "backups"}
# Exact public-source exceptions (writer proposal 0345): exact case-sensitive path +
# exact sha256. An exception waives ONLY the single rule named below for that exact
# path; every other check stays mandatory (casefold deny on the other components,
# traversal/device/key rules, ancestor-symlink and resolved-containment checks before
# any IO, PEM content scan). The pinned hash never bypasses a filesystem check, and
# nothing else under var/data/backup trees or any other env file is allowed: no
# general backup/env allowance. A changed template needs fresh validation.
RUNTIME_TREE_EXCEPTIONS = {
    # scheduler source, writer-pinned
    "scripts/backup/run_backup.py":
        "fad4417044069ab6b777521e0d5d89d87ccb45f210fe0fcdf7ea1d45b17afbf0",
}
ENV_EXAMPLE_PINNED = {
    # fixed public template (APP_NAME/APP_ENV development, secret fields empty),
    # writer-pinned; any other .env.example bytes face the placeholder policy
    ".env.example":
        "8fd99031e6d85cb6176d711bba702b717e8a7a7ca8b761a5af4f6d5d8ece2e35",
}
KEY_CERT_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".crt", ".cer", ".der", ".jks", ".keystore"}
KEY_CERT_NAMES = {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
PRIVATE_KEY_MARKERS = (b"-----BEGIN RSA PRIVATE KEY-----", b"-----BEGIN EC PRIVATE KEY-----",
                       b"-----BEGIN DSA PRIVATE KEY-----", b"-----BEGIN OPENSSH PRIVATE KEY-----",
                       b"-----BEGIN PRIVATE KEY-----", b"-----BEGIN ENCRYPTED PRIVATE KEY-----",
                       b"-----BEGIN PGP PRIVATE KEY BLOCK-----", b"-----BEGIN CERTIFICATE-----")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Conservative NEW .env.example policy (hypothesis, fails closed): every value must be
# empty or obviously a public placeholder. Anything else is refused, not heuristically
# accepted; loosening it needs an explicit reviewed decision.
ENV_PLACEHOLDER_RE = re.compile(
    r"^(|true|false|yes|no|on|off|0|[1-9][0-9]{0,9}|localhost|127\.0\.0\.1|0\.0\.0\.0|"
    r".*[<][^<>]*[>].*|.*(example|placeholder|sample|dummy|changeme|change[-_]me|your[-_]).*)$",
    re.IGNORECASE)
PATH_IDENTITY_LIMIT = (
    "lexical-only: a string policy cannot disprove junction, reparse-point or 8.3 "
    "short-name identity; a lexical path is never a native identity. Unresolved by design.")
NOTE = ("Non-operational staging document tree. Executes nothing, installs nothing, "
        "downloads nothing. Not whole-system atomic: the completion marker is "
        "last-written, not a transaction.")


# ------------------------------------------------------------------ small helpers
def _sha256_bytes(b):
    return hashlib.sha256(b).hexdigest()


def _is_real_dir(p):
    return p.is_dir() and not p.is_symlink()


def _is_real_file(p):
    return p.is_file() and not p.is_symlink()


def _inside(child, parent):
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def check_source_fs_path(src, rel):
    """Filesystem-side safety for one manifest-listed source path, run BEFORE any IO
    on the listed file: every ancestor component under the source root must be an
    existing real (non-symlink) directory, and the resolved path must stay inside the
    resolved source root. An explicit manifest entry with a matching hash must never
    read outside the source root through a symlinked ancestor (e.g. app -> outside).
    The pinned-hash exceptions never bypass this check. rel must already have passed
    check_source_relpath."""
    src = Path(src)
    if src.is_symlink() or not src.is_dir():
        raise StageError("source root is not a real (non-symlink) directory")
    comps = rel.split("/")
    p = src
    for c in comps[:-1]:
        p = p / c
        if p.is_symlink():
            raise StageError(f"{rel}: symlinked source ancestor refused ({c!r}); "
                             "no out-of-root read")
        if not p.is_dir():
            raise StageError(f"{rel}: source ancestor is not a real directory ({c!r})")
    leaf = p / comps[-1]
    if not _inside(leaf, src):
        raise StageError(f"{rel}: resolved path escapes the source root; refused")
    return leaf


# ------------------------------------------------------------------ source manifest
def check_source_relpath(path):
    """Strict NEW relative source path (posix separators). Refuses absolute, traversal,
    backslash/drive forms, empty/dot components, control chars, trailing dot/space
    components, Windows device names, and every policy-listed tree (env, runtime,
    VCS/cache/venv, keys/certs). Every deny comparison is CASEFOLDED: Windows is
    case-insensitive, so app/.ENV, app/.Git/config and app/VAR/secret.txt are the
    denied .env, .git and var trees there and must refuse here too. The only waiver is
    an exact case-sensitive path in RUNTIME_TREE_EXCEPTIONS (writer-pinned hash
    enforced at manifest load). Pure string check, no IO. Lexical policy only."""
    if not isinstance(path, str) or not path or len(path) > 512:
        raise StageError("source path must be a non-empty string (<=512 chars)")
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise StageError(f"control character in path {path!r}")
    if "\\" in path or ":" in path:
        raise StageError(f"backslash or drive/ADS colon not allowed in source path {path!r}")
    if path.startswith("/") or path.startswith("~"):
        raise StageError(f"absolute path not allowed in source manifest: {path!r}")
    comps = path.split("/")
    if any(c in ("", ".", "..") for c in comps):
        raise StageError(f"empty/dot/traversal component in source path {path!r}")
    for c in comps:
        if c != c.strip() or c.endswith(".") or c.endswith(" "):
            raise StageError(f"trailing dot/space component refused in {path!r}")
        if c.split(".", 1)[0].upper() in hs.WIN_DEVICE_STEMS:
            raise StageError(f"Windows device name refused in {path!r}")
        cf = c.casefold()  # Windows is case-insensitive: deny checks must be too
        if cf in VCS_CACHE_VENV:
            raise StageError(f"VCS/cache/venv component refused in {path!r}")
        if cf in RUNTIME_TREE_NAMES and path not in RUNTIME_TREE_EXCEPTIONS:
            raise StageError(f"runtime var/data/backup tree component refused in {path!r}")
        if cf == ".env.example" and path != ".env.example":
            raise StageError(f"only exact root public template is allowed: {path!r}")
        if cf == ".env" or (cf.startswith(".env.") and cf != ".env.example"):
            raise StageError(f"actual env file refused (only .env.example is allowed): {path!r}")
        if cf in KEY_CERT_NAMES or Path(c).suffix.lower() in KEY_CERT_SUFFIXES:
            raise StageError(f"private key/certificate file refused: {path!r}")
    root = comps[0]
    if not (root in ALLOWED_DIR_ROOTS or (len(comps) == 1 and path in ALLOWED_FILE_ROOTS)):
        raise StageError(f"path outside the allowed app source roots: {path!r}")
    return comps


def check_env_example(text):
    """`.env.example` is the sole env-file exception and must be a public placeholder
    only. Conservative NEW policy: KEY=VALUE lines only, value empty or matching the
    placeholder pattern; anything else fails closed."""
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            raise StageError(f".env.example line {n}: not KEY=VALUE or comment")
        key, _, value = s.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not ENV_KEY_RE.match(key):
            raise StageError(f".env.example line {n}: bad key {key!r}")
        if ENV_PLACEHOLDER_RE.match(value) is None:
            raise StageError(
                f".env.example line {n}: value for {key} does not look like a public "
                "placeholder; refusing rather than guessing (reviewed decision needed)")


def check_file_content(path, data):
    """Content-level refusals: private key / certificate PEM markers anywhere (never
    waived, not even for the pinned exceptions), and the .env.example placeholder
    policy. The placeholder policy is skipped only for the exact writer-pinned
    template bytes (ENV_EXAMPLE_PINNED); any changed template faces the policy again
    and needs fresh validation, not silent acceptance."""
    for marker in PRIVATE_KEY_MARKERS:
        if marker in data:
            raise StageError(f"{path}: private key/certificate PEM marker in content")
    if path == ".env.example" and _sha256_bytes(data) != ENV_EXAMPLE_PINNED[".env.example"]:
        check_env_example(data.decode("utf-8", errors="strict"))


def load_source_manifest(path):
    """Parse and fully validate the NEW explicit source manifest (no IO on listed files)."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise StageError(f"source manifest unreadable: {e}")
    if not isinstance(doc, dict) or doc.get("schema") != SRCMAN_FORMAT:
        raise StageError(f"source manifest schema must be {SRCMAN_FORMAT!r}")
    if doc.get("version") != SRCMAN_VERSION or isinstance(doc.get("version"), bool):
        raise StageError(f"unsupported source manifest version {doc.get('version')!r}")
    files = doc.get("files")
    if not isinstance(files, list) or not files:
        raise StageError("source manifest files must be a non-empty list")
    entries = {}
    for i, e in enumerate(files):
        if not isinstance(e, dict) or set(e) != {"path", "sha256"}:
            raise StageError(f"files[{i}]: exactly {{path, sha256}} required")
        if not isinstance(e["path"], str) or not isinstance(e["sha256"], str):
            raise StageError(f"files[{i}]: path and sha256 must be strings")
        check_source_relpath(e["path"])
        if hs.HEX64.fullmatch(e["sha256"]) is None:
            raise StageError(f"files[{i}]: sha256 must be 64 lowercase hex")
        if e["path"] in RUNTIME_TREE_EXCEPTIONS and \
                e["sha256"] != RUNTIME_TREE_EXCEPTIONS[e["path"]]:
            raise StageError(
                f"files[{i}]: {e['path']!r} is allowed only as the writer-reviewed "
                "public source with its pinned sha256; refusing the changed/unknown "
                "bytes rather than allowing the path generally")
        if e["path"] in entries:
            raise StageError(f"duplicate manifest path {e['path']!r}")
        entries[e["path"]] = e["sha256"]
    folded = {}
    for p in entries:
        k = p.casefold()
        if k in folded:
            raise StageError(f"case-collision between {folded[k]!r} and {p!r}")
        folded[k] = p
    for req in REQUIRED_FILE_ROOTS:
        if req not in entries:
            raise StageError(f"required app source file missing from manifest: {req}")
    for d in REQUIRED_DIR_ROOTS:
        if not any(p.startswith(d + "/") for p in entries):
            raise StageError(f"required app source directory has no files in manifest: {d}/")
    return entries


def load_runtime_inventory(path):
    """Parse the NEW runtime inventory document and validate it against the recovered,
    unchanged ISS consumer contract (exactly [0] exit codes, reviewed non-empty
    silent_args, safe bare filenames, pinned sha256)."""
    try:
        doc = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise StageError(f"runtime inventory unreadable: {e}")
    if not isinstance(doc, dict) or doc.get("schema") != INVENTORY_FORMAT:
        raise StageError(f"runtime inventory schema must be {INVENTORY_FORMAT!r}")
    if doc.get("version") != INVENTORY_VERSION or isinstance(doc.get("version"), bool):
        raise StageError(f"unsupported runtime inventory version {doc.get('version')!r}")
    runtimes = doc.get("runtimes")
    if not isinstance(runtimes, list) or not runtimes:
        raise StageError("runtime inventory runtimes must be a non-empty list")
    for i, it in enumerate(runtimes):
        if not isinstance(it, dict) or set(it) != {"key", "filename", "sha256",
                                                   "ok_exit_codes", "silent_args"}:
            raise StageError(f"runtimes[{i}]: exactly {{key, filename, sha256, "
                             "ok_exit_codes, silent_args}} required")
    hs.validate_inventory(runtimes)
    return runtimes


def strict_wheelhouse_tree(wheelhouse_dir, reqs):
    """Complete strict tree check: the wheelhouse contains exactly the locked .whl
    files and nothing else (no subdirectories, no unlisted artifacts, no symlinks),
    and every wheel matches name/version/sha256 from the hashed requirements."""
    d = Path(wheelhouse_dir)
    problems = hs.verify_hashed_wheelhouse(reqs, d)
    for p in sorted(d.iterdir()):
        if p.is_symlink():
            problems.append(f"{p.name}: symlink refused in wheelhouse")
        elif p.is_dir():
            problems.append(f"{p.name}: subdirectory refused in wheelhouse")
        elif not p.is_file():
            problems.append(f"{p.name}: special file refused in wheelhouse")
        elif p.suffix != ".whl":
            problems.append(f"{p.name}: unlisted non-wheel artifact in wheelhouse")
    return problems


def check_inputs_tree(inputs_dir, inventory):
    """The inputs dir must contain exactly the inventory filenames, each regular,
    non-symlink and hash-matching; no unlisted files."""
    problems = hs.verify_inputs(inputs_dir, inventory)
    listed = {it["filename"] for it in inventory}
    d = Path(inputs_dir)
    for p in sorted(d.iterdir()):
        if p.is_symlink() or not p.is_file():
            problems.append(f"{p.name}: not a regular non-symlink input file")
        elif p.name not in listed:
            problems.append(f"{p.name}: unlisted file in inputs dir")
    return problems


# ------------------------------------------------------------------ tree rel paths
def check_tree_relpath(path):
    """Relative path inside the stage tree (posix separators, inno/ root)."""
    if not isinstance(path, str) or not path or "\\" in path or ":" in path:
        raise StageError(f"bad tree path {path!r}")
    comps = path.split("/")
    if any(c in ("", ".", "..") for c in comps):
        raise StageError(f"empty/dot/traversal component in tree path {path!r}")
    if comps[0] != INNO_DIR:
        raise StageError(f"tree path outside {INNO_DIR}/: {path!r}")
    if any(any(ord(ch) < 32 for ch in c) for c in comps):
        raise StageError(f"control character in tree path {path!r}")
    return comps


# ------------------------------------------------------------------ copy + hash
def _copy_hashed(src, dst, expect_sha256=None):
    """Copy one regular file, hashing the bytes read and re-hashing the bytes written.
    Refuses (StageError) on any mismatch; raises OSError on IO failure."""
    data = Path(src).read_bytes()
    got = _sha256_bytes(data)
    if expect_sha256 is not None and got != expect_sha256:
        raise StageError(f"sha256 mismatch for {Path(src).name}: {got} != {expect_sha256}")
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes(data)
    if _sha256_bytes(dst.read_bytes()) != got:
        raise StageError(f"post-write re-hash mismatch for {dst.name}")
    return got, len(data)


# ------------------------------------------------------------------ documents
def build_stage_manifest(entries, staged_version):
    """NEW stage manifest: every file under inno/ with sha256 and byte size."""
    return {"schema": STAGE_MAN_FORMAT, "version": STAGE_MAN_VERSION,
            "status": "NEW - not a recovered format",
            "staged_version": staged_version,
            "non_operational": True, "note": NOTE, "path_identity": PATH_IDENTITY_LIMIT,
            "files": [{"path": p, "sha256": h, "bytes": n}
                      for p, h, n in sorted(entries)]}


def _write_json(path, doc):
    Path(path).write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_state(dest, state, reason=None, manifest_sha256=None):
    doc = {"schema": STATE_FORMAT, "version": STATE_VERSION, "state": state,
           "non_operational": True, "note": NOTE}
    if reason is not None:
        doc["reason"] = reason
    if manifest_sha256 is not None:
        doc["manifest_sha256"] = manifest_sha256
    tmp = Path(dest) / (STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, Path(dest) / STATE_FILE)  # atomic rename of our own marker, no deletion


# ------------------------------------------------------------------ validation (pre-create)
def validate_all(source_root, manifest_entries, wheelhouse, requirements_text,
                 inventory, inputs_dir):
    """Every check that needs no destination. Fails BEFORE the destination is created,
    so a refused run leaves no tree at all. Returns (reqs, planned file copies)."""
    problems = []
    # requirements + wheelhouse
    try:
        reqs = hs.parse_hashed_requirements(requirements_text)
    except StageError as e:
        raise StageError(f"requirements: {e}")
    problems += strict_wheelhouse_tree(wheelhouse, reqs)
    # inputs
    problems += check_inputs_tree(inputs_dir, inventory)
    # source files: exist, regular non-symlink, hash match, content policy
    src = Path(source_root)
    copies = []
    for rel, want in sorted(manifest_entries.items()):
        try:
            # every-ancestor non-symlink + resolved containment BEFORE any IO/copy:
            # an explicit manifest + matching hash must not read outside the root
            p = check_source_fs_path(src, rel)
        except StageError as e:
            problems.append(str(e))
            continue
        if p.is_symlink():
            problems.append(f"{rel}: symlink refused")
            continue
        if not p.exists():
            problems.append(f"{rel}: listed in manifest but missing (fail closed)")
            continue
        if not p.is_file():
            problems.append(f"{rel}: not a regular file (special files refused)")
            continue
        try:
            data = p.read_bytes()
        except OSError as e:
            problems.append(f"{rel}: unreadable: {e}")
            continue
        if _sha256_bytes(data) != want:
            problems.append(f"{rel}: sha256 mismatch against manifest")
            continue
        try:
            check_file_content(rel, data)
        except StageError as e:
            problems.append(str(e))
            continue
        copies.append((rel, data))
    if problems:
        raise StageError("; ".join(problems))
    return reqs, copies


# ------------------------------------------------------------------ stage-tree
def stage_tree(source_root, source_manifest, wheelhouse, requirements, runtime_inventory,
               inputs, destination, version):
    dest = Path(destination)
    if os.path.lexists(dest):
        raise StageError("destination already exists: no overwrite, no reuse, no merge")
    if dest.is_symlink():
        raise StageError("destination is a symlink")
    if not _is_real_dir(dest.parent):
        raise StageError("destination parent must be an existing real directory")
    for d in (source_root, wheelhouse, inputs):
        if not _is_real_dir(Path(d)):
            raise StageError(f"not a real (non-symlink) directory: {d}")
    for f in (source_manifest, requirements, runtime_inventory):
        if not _is_real_file(Path(f)):
            raise StageError(f"not a real (non-symlink) file: {f}")
    for d in (source_root, wheelhouse, inputs):
        if _inside(dest, Path(d)) or _inside(Path(d), dest):
            raise StageError(f"destination overlaps input directory {d}")
    if hs.VERSION_RE.match(str(version)) is None:
        raise StageError("bad --version value")

    manifest_entries = load_source_manifest(source_manifest)
    inventory = load_runtime_inventory(runtime_inventory)
    requirements_text = Path(requirements).read_text(encoding="utf-8")
    reqs, copies = validate_all(source_root, manifest_entries, wheelhouse,
                                requirements_text, inventory, inputs)

    # Everything validated. Create the tree; mark it incomplete from the first byte.
    inno = dest / INNO_DIR
    dest.mkdir()
    _write_state(dest, "incomplete", reason="staging in progress")
    entries = []
    try:
        for rel, data in copies:  # app source, manifest-driven only
            dst = inno / "app-src" / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(data)
            if _sha256_bytes(dst.read_bytes()) != manifest_entries[rel]:
                raise StageError(f"post-write re-hash mismatch for app-src/{rel}")
            entries.append((f"{INNO_DIR}/app-src/{rel}", manifest_entries[rel], len(data)))
        wh_out = inno / "wheelhouse"
        wh_out.mkdir()
        for w in sorted(Path(wheelhouse).glob("*.whl")):
            h, n = _copy_hashed(w, wh_out / w.name, reqs[hs.wheel_pin(w.name)[0]]["sha256"])
            entries.append((f"{INNO_DIR}/wheelhouse/{w.name}", h, n))
        req_bytes = requirements_text.encode("utf-8")
        h, n = _copy_hashed(requirements, inno / "requirements-locked.txt",
                            _sha256_bytes(req_bytes))
        entries.append((f"{INNO_DIR}/requirements-locked.txt", h, n))
        inp_out = inno / "inputs"
        inp_out.mkdir()
        for it in inventory:
            h, n = _copy_hashed(Path(inputs) / it["filename"], inp_out / it["filename"],
                                it["sha256"].lower())
            entries.append((f"{INNO_DIR}/inputs/{it['filename']}", h, n))
        checks = inno / "installer-checks"
        checks.mkdir()
        verify_install_src = Path(__file__).resolve().parent / "verify_install.py"
        if not _is_real_file(verify_install_src):
            raise StageError("bundled verify_install.py missing or not a regular file")
        h, n = _copy_hashed(verify_install_src, checks / "verify_install.py")
        entries.append((f"{INNO_DIR}/installer-checks/verify_install.py", h, n))
        docs = {"install-plan.json": hs.build_install_plan(inventory),
                "dependency-inventory.json": hs.dependency_inventory(inventory),
                "ownership-state.template.json": hs.ownership_template(),
                "wheels.lock.json": hs.build_wheels_lock(reqs, wheelhouse)}
        for name, doc in sorted(docs.items()):
            data = (json.dumps(doc, indent=2, sort_keys=True) + "\n").encode("utf-8")
            (checks / name).write_bytes(data)
            entries.append((f"{INNO_DIR}/installer-checks/{name}", _sha256_bytes(data),
                            len(data)))
        inc = hs.generate_iss_include(inventory, version).encode("utf-8")
        (inno / "staged-inputs.iss.inc").write_bytes(inc)
        entries.append((f"{INNO_DIR}/staged-inputs.iss.inc", _sha256_bytes(inc), len(inc)))
        manifest_doc = build_stage_manifest(entries, version)
        _write_json(dest / MANIFEST_FILE, manifest_doc)
        man_hash = _sha256_bytes((dest / MANIFEST_FILE).read_bytes())
        # The completion marker is NOT written here: the candidate stays marked
        # incomplete until it has been verified (see below).
    except (StageError, OSError) as e:
        # NO cleanup deletion: the partial tree stays, marked incomplete, for
        # writer inspection. verify-tree refuses it.
        _write_state(dest, "incomplete", reason=f"staging failed: {e}")
        raise StageError(f"staging failed mid-write (tree left in place, marked "
                         f"incomplete; inspect and remove manually): {e}")

    report = verify_tree(dest, _pending_manifest_sha256=man_hash)
    if report["status"] != "pass":  # exact tree re-verified before claiming staged
        _write_state(dest, "incomplete",
                     reason="self-verification failed: " + "; ".join(report["refusal_reasons"]))
        raise StageError("self-verification of the new tree failed: "
                         + "; ".join(report["refusal_reasons"]))
    # Verified as an incomplete candidate: the completion marker is the LAST write.
    # Still no whole-system atomicity claim: last-written marker, not a transaction.
    _write_state(dest, "complete", manifest_sha256=man_hash)
    return {"format": STAGE_RESULT_FORMAT, "version": 1, "status": "staged", "staged_version": version,
            "destination": str(dest), "files": len(entries), "refusal_reasons": [],
            "non_operational": True, "note": NOTE, "path_identity": PATH_IDENTITY_LIMIT,
            "unverified": "UNVERIFIED against the real repo; hypothesis until checked on Windows"}


# ------------------------------------------------------------------ verify-tree
def _walk_files(root):
    """All files under root, refusing symlinks and special files. Read-only."""
    found, problems = [], []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dp = Path(dirpath)
        for d in list(dirnames):
            if (dp / d).is_symlink():
                problems.append(f"symlinked directory refused: {(dp / d).relative_to(root)}")
                dirnames.remove(d)
        for f in filenames:
            p = dp / f
            rel = p.relative_to(root).as_posix()
            if p.is_symlink():
                problems.append(f"symlink refused: {rel}")
            elif not p.is_file():
                problems.append(f"special file refused: {rel}")
            else:
                found.append(rel)
    return sorted(found), problems


def verify_tree(root, _pending_manifest_sha256=None):
    """Read-only exact verification of a stage tree. Refuses incomplete trees (missing
    or non-complete state marker), any changed/missing/extra byte, any symlink, and any
    content-contract mismatch (lock vs wheelhouse vs requirements, include vs
    inventory, inputs vs inventory).

    _pending_manifest_sha256 is internal to stage-tree's self-check: the candidate is
    verified while still marked INCOMPLETE (completion marker written only after this
    passes) and its manifest hash must equal the pending value. Public verify-tree
    callers never set it and keep requiring the complete marker."""
    pending = _pending_manifest_sha256 is not None
    problems, checks = [], []
    root = Path(root)
    if not root.exists() or not _is_real_dir(root):
        problems.append("root is not an existing real (non-symlink) directory")
        return _verify_report(problems, checks, None)
    top = {p.name for p in root.iterdir()}
    if not top <= TOP_LEVEL:
        problems.append("unexpected top-level entries: " + ", ".join(sorted(top - TOP_LEVEL)))
    if STATE_FILE not in top or MANIFEST_FILE not in top or INNO_DIR not in top:
        problems.append("incomplete tree: missing state file, manifest or inno/ directory")
        return _verify_report(problems, checks, None)
    # state marker
    staged_version = None
    try:
        state = json.loads((root / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        problems.append(f"state file unreadable: {e}")
        return _verify_report(problems, checks, None)
    if not isinstance(state, dict) or state.get("schema") != STATE_FORMAT \
            or state.get("version") != STATE_VERSION or isinstance(state.get("version"), bool):
        problems.append("bad state marker schema/version")
    elif pending:
        if state.get("state") != "incomplete":
            problems.append(f"candidate must still be marked incomplete before the "
                            f"completion write, got {state.get('state')!r}")
    elif state.get("state") != "complete":
        problems.append(f"tree is marked incomplete ({state.get('reason', 'no reason')}); "
                        "requires writer inspection")
    checks.append("completion-marker")
    # manifest hash pinned by the state marker
    man_path = root / MANIFEST_FILE
    if not _is_real_file(man_path):
        problems.append("stage manifest missing or not a regular non-symlink file")
        return _verify_report(problems, checks, staged_version)
    man_bytes = man_path.read_bytes()
    if pending:
        if _sha256_bytes(man_bytes) != _pending_manifest_sha256:
            problems.append("stage manifest hash does not match the pending completion value")
    elif isinstance(state, dict) and state.get("state") == "complete":
        if _sha256_bytes(man_bytes) != state.get("manifest_sha256"):
            problems.append("stage manifest hash does not match the completion marker")
    checks.append("manifest-hash-pin")
    try:
        manifest = json.loads(man_bytes.decode("utf-8"))
    except ValueError as e:
        problems.append(f"stage manifest malformed: {e}")
        return _verify_report(problems, checks, staged_version)
    if not isinstance(manifest, dict) or manifest.get("schema") != STAGE_MAN_FORMAT \
            or manifest.get("version") != STAGE_MAN_VERSION \
            or isinstance(manifest.get("version"), bool):
        problems.append("bad stage manifest schema/version")
    staged_version = manifest.get("staged_version") if isinstance(manifest, dict) else None
    if not isinstance(staged_version, str) or hs.VERSION_RE.match(staged_version) is None:
        problems.append(f"bad staged_version {staged_version!r}")
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, list) or not files:
        problems.append("stage manifest files must be a non-empty list")
        files = []
    entries = {}
    folded = {}
    for i, e in enumerate(files):
        if not isinstance(e, dict) or set(e) != {"path", "sha256", "bytes"} \
                or not isinstance(e.get("path"), str) or not isinstance(e.get("sha256"), str) \
                or not isinstance(e.get("bytes"), int) or isinstance(e.get("bytes"), bool):
            problems.append(f"manifest files[{i}]: bad entry")
            continue
        try:
            check_tree_relpath(e["path"])
        except StageError as ex:
            problems.append(f"manifest files[{i}]: {ex}")
            continue
        if hs.HEX64.fullmatch(e["sha256"]) is None:
            problems.append(f"manifest files[{i}]: bad sha256")
            continue
        if e["path"] in entries:
            problems.append(f"duplicate manifest path {e['path']!r}")
            continue
        k = e["path"].casefold()
        if k in folded:
            problems.append(f"case-collision between {folded[k]!r} and {e['path']!r}")
            continue
        folded[k] = e["path"]
        entries[e["path"]] = (e["sha256"], e["bytes"])
    checks.append("manifest-schema")
    # exact tree: every file listed and matching, no unexpected files
    found, walk_problems = _walk_files(root / INNO_DIR) if (root / INNO_DIR).is_dir() else ([], [])
    problems += walk_problems
    found = [f"{INNO_DIR}/{f}" for f in found]
    for rel in found:
        if rel not in entries:
            problems.append(f"unexpected file in tree: {rel}")
    for rel, (want_h, want_n) in entries.items():
        if rel not in found:
            problems.append(f"missing file: {rel}")
            continue
        p = root / rel
        data = p.read_bytes()
        if len(data) != want_n:
            problems.append(f"size changed: {rel}")
        if _sha256_bytes(data) != want_h:
            problems.append(f"content changed: {rel}")
    checks.append("exact-tree-bytes")
    if problems:
        return _verify_report(problems, checks, staged_version)
    # content contracts (all present at this point)
    inno = root / INNO_DIR
    try:
        reqs = hs.parse_hashed_requirements(
            (inno / "requirements-locked.txt").read_text(encoding="utf-8"))
        checks.append("requirements-strict-parse")
        problems += strict_wheelhouse_tree(inno / "wheelhouse", reqs)
        checks.append("wheelhouse-exact")
        lock = json.loads((inno / "installer-checks" / "wheels.lock.json").read_text("utf-8"))
        problems += hs.verify_wheels_lock(lock, inno / "wheelhouse")
        lock_wheels = {(w["name"], w["version"], w["sha256"]) for w in lock.get("wheels", [])
                       if isinstance(w, dict)}
        req_wheels = {(n, r["version"], r["sha256"]) for n, r in reqs.items()}
        if lock_wheels != req_wheels:
            problems.append("wheels.lock.json disagrees with requirements-locked.txt")
        checks.append("wheels-lock")
        depinv = json.loads((inno / "installer-checks" / "dependency-inventory.json")
                            .read_text("utf-8"))
        runtimes = depinv.get("runtimes")
        if depinv.get("format") != hs.DEPINV_FORMAT or not isinstance(runtimes, list):
            problems.append("dependency-inventory.json malformed")
        else:
            hs.validate_inventory(runtimes)
            problems += check_inputs_tree(inno / "inputs", runtimes)
            want_inc = hs.generate_iss_include(runtimes, staged_version)
            got_inc = (inno / "staged-inputs.iss.inc").read_text(encoding="utf-8")
            if got_inc != want_inc:
                problems.append("staged-inputs.iss.inc does not match the inventory+version")
            checks.append("inputs-exact")
            checks.append("include-matches-inventory")
        for name in ("install-plan.json", "ownership-state.template.json"):
            doc = json.loads((inno / "installer-checks" / name).read_text("utf-8"))
            if not isinstance(doc, dict) or doc.get("operational") is not False:
                problems.append(f"{name}: malformed or claims to be operational")
        checks.append("plan-documents")
        vi = inno / "installer-checks" / "verify_install.py"
        if not _is_real_file(vi):
            problems.append("verify_install.py missing or not a regular non-symlink file")
        checks.append("verify-install-present")
        # app-src required members
        for req in REQUIRED_FILE_ROOTS:
            if f"{INNO_DIR}/app-src/{req}" not in entries:
                problems.append(f"app-src missing required {req}")
        for d in REQUIRED_DIR_ROOTS:
            if not any(p.startswith(f"{INNO_DIR}/app-src/{d}/") for p in entries):
                problems.append(f"app-src missing required directory content {d}/")
        checks.append("app-src-required")
    except (StageError, OSError, ValueError, KeyError, AttributeError) as e:
        problems.append(f"content-contract check failed: {e.__class__.__name__}: {e}")
    return _verify_report(problems, checks, staged_version)


def _verify_report(problems, checks, staged_version):
    return {"format": VERIFY_RESULT_FORMAT, "version": 1,
            "status": "refused" if problems else "pass",
            "staged_version": staged_version,
            "refusal_reasons": problems,
            "checks": checks,
            "non_operational": True, "note": NOTE, "path_identity": PATH_IDENTITY_LIMIT,
            "unverified": "UNVERIFIED against the real repo; hypothesis until checked on Windows"}


# ------------------------------------------------------------------ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="hony_stagetree",
        description="NEW offline source-stage tree builder/verifier (non-operational)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("stage-tree", help="create a NEW stage tree (destination must not exist)")
    st.add_argument("--source-root", required=True)
    st.add_argument("--source-manifest", required=True)
    st.add_argument("--wheelhouse", required=True)
    st.add_argument("--requirements", required=True)
    st.add_argument("--runtime-inventory", required=True)
    st.add_argument("--inputs", required=True)
    st.add_argument("--destination", required=True)
    st.add_argument("--version", required=True)
    vt = sub.add_parser("verify-tree", help="read-only verification of a stage tree")
    vt.add_argument("--root", required=True)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "stage-tree":
            doc = stage_tree(a.source_root, a.source_manifest, a.wheelhouse, a.requirements,
                             a.runtime_inventory, a.inputs, a.destination, a.version)
        else:
            doc = verify_tree(a.root)
    except StageError as e:
        fmt = STAGE_RESULT_FORMAT if a.cmd == "stage-tree" else VERIFY_RESULT_FORMAT
        doc = {"format": fmt, "version": 1, "status": "refused", "refusal_reasons": [str(e)],
               "non_operational": True, "note": NOTE, "path_identity": PATH_IDENTITY_LIMIT,
               "unverified": "UNVERIFIED against the real repo; hypothesis until checked on Windows"}
    except (OSError, ValueError) as e:
        fmt = STAGE_RESULT_FORMAT if a.cmd == "stage-tree" else VERIFY_RESULT_FORMAT
        doc = {"format": fmt, "version": 1, "status": "refused",
               "refusal_reasons": [f"{e.__class__.__name__}: {e}"],
               "non_operational": True, "note": NOTE, "path_identity": PATH_IDENTITY_LIMIT,
               "unverified": "UNVERIFIED against the real repo; hypothesis until checked on Windows"}
    print(json.dumps(doc, indent=2, sort_keys=True))
    return 0 if doc["status"] in ("staged", "pass") else 2


if __name__ == "__main__":
    import sys
    sys.exit(main())
