"""Deterministic dependency-closure walker over explicitly declared snapshot roots.

Seeds and roots are explicit; nothing resolves through CWD, PATH or the OS
loader. Case-insensitive file-name matching inside declared roots only.
Every edge is kept with importer, kind, requested name and outcome.

Limits of meaning:
- Resolution is a candidate model over declared roots in declared order, NOT
  the Windows loader's search and NOT the file the OS would select.
- Roots are meant to be explicit, bounded offline snapshots, not live System32.
  Indexing is NON-recursive by default (a loader does not search subfolders of
  an app directory by name); recursive indexing is opt-in and bounded.
- Export forwarders of every walked module are followed, whether or not any
  importer uses that symbol: forwarder edges are a conservative
  over-approximation, not the actual required dependency closure.
- 'resolved-via-supplied-mapping' means only: a caller-supplied, unverified
  mapping named a host file and a same-named file exists in the declared roots.
  It does not claim native API-set resolution.
"""
from __future__ import annotations

import hashlib
import json
import os
from collections import deque
from typing import Dict, List, Optional, Tuple

from . import apiset
from .pe import (MACHINE_NAMES, PEFormatError, parse_forwarder, parse_pe, read_capped,
                 read_machine)
from . import pe as _pe

REPORT_SCHEMA = "pe-closure/report@2"
DEFAULT_MAX_NODES = 10000
DEFAULT_MAX_DEPTH = 256
DEFAULT_MAX_INDEX_FILES = 20000
DEFAULT_MAX_INDEX_DEPTH = 8
DEFAULT_MAX_FILE_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_TOTAL_BYTES = 1024 * 1024 * 1024

NOTICE = ("Static analysis over explicitly declared snapshot roots in declared order. "
          "Candidates are not the OS loader's selected files; static presence and "
          "parse success do not establish readability under a restricted token, "
          "loader policy, successful load, or network denial. Export-forwarder edges "
          "are a conservative over-approximation (all forwarders of walked modules). "
          "No native regression against a real python.exe is implied.")


def is_apiset_name(name: str) -> bool:
    lower = name.lower()
    return lower.startswith("api-ms-") or lower.startswith("ext-ms-")


def forwarder_target(forwarder: str) -> Optional[str]:
    """DLL file name a forwarder points at, or None when unparseable.

    'NTDLL.RtlFoo' / 'NTDLL.#42' -> 'NTDLL.dll'. Split at the last dot; a
    module part that contains a dot (extension or ambiguity) is rejected, not
    truncated. Appending '.dll' is a modeling convention, not loader behavior.
    """
    parsed = parse_forwarder(forwarder)
    return None if parsed is None else parsed[0] + ".dll"


class _Budget:
    def __init__(self, max_file, max_total):
        self.max_file = max_file
        self.max_total = max_total
        self.used = 0

    def read(self, path: str) -> bytes:
        remaining = self.max_total - self.used
        if remaining <= 0:
            raise PEFormatError("refused: total read budget %d bytes exhausted" % self.max_total)
        data = read_capped(path, min(self.max_file, remaining))
        self.used += len(data)
        return data


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def build_index(roots: List[Dict], recursive: bool = False,
                max_files: int = DEFAULT_MAX_INDEX_FILES,
                max_depth: int = DEFAULT_MAX_INDEX_DEPTH) -> Tuple[Dict[str, List[Dict]], List[Dict]]:
    """Lowercase basename -> candidates (names only; NO content is read or hashed here).

    Returns (index, notes). Notes record every skipped/unreadable entry and
    any truncation, so omissions are visible in the report.
    """
    index: Dict[str, List[Dict]] = {}
    notes: List[Dict] = []
    count = 0
    for order, root in enumerate(roots):
        stack = [(root["path"], 0)]
        while stack:
            dirpath, depth = stack.pop()
            try:
                with os.scandir(dirpath) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError as exc:
                notes.append({"root": root["label"], "path": dirpath,
                              "reason": "directory-unreadable: %s" % exc.__class__.__name__})
                continue
            subdirs = []
            for ent in entries:
                full = os.path.join(dirpath, ent.name)
                try:
                    if ent.is_symlink():
                        notes.append({"root": root["label"], "path": full,
                                      "reason": "skipped-symlink"})
                        continue
                    if ent.is_dir(follow_symlinks=False):
                        if recursive:
                            if depth + 1 > max_depth:
                                notes.append({"root": root["label"], "path": full,
                                              "reason": "skipped-depth-bound"})
                            else:
                                subdirs.append((full, depth + 1))
                        continue
                    if not ent.is_file(follow_symlinks=False):
                        notes.append({"root": root["label"], "path": full,
                                      "reason": "skipped-not-regular-file"})
                        continue
                except OSError as exc:
                    notes.append({"root": root["label"], "path": full,
                                  "reason": "stat-failed: %s" % exc.__class__.__name__})
                    continue
                if count >= max_files:
                    notes.append({"root": root["label"], "path": full,
                                  "reason": "skipped-index-file-count-bound-%d" % max_files})
                    continue
                count += 1
                index.setdefault(ent.name.lower(), []).append(
                    {"path": full, "root_label": root["label"], "root_order": order})
            stack.extend(reversed(subdirs))
    for entries in index.values():
        entries.sort(key=lambda e: (e["root_order"], e["path"].lower()))
    return index, notes


def run_closure(seeds: List[str], roots: List[Dict],
                mapping_doc: Optional[Dict] = None,
                recursive: bool = False,
                max_nodes: int = DEFAULT_MAX_NODES,
                max_depth: int = DEFAULT_MAX_DEPTH,
                max_index_files: int = DEFAULT_MAX_INDEX_FILES,
                max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
                max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES) -> Dict:
    """mapping_doc is a decoded pe-closure/apiset-mapping@1 document; it is
    validated here through the public API (not only by the CLI)."""
    if not roots:
        raise ValueError("at least one explicit root is required")
    mapping = None
    mapping_info = None
    if mapping_doc is not None:
        mapping, digest = apiset.validate_document(mapping_doc)
        mapping_info = {"schema": apiset.SCHEMA, "canonical_sha256": digest,
                        "entries": len(mapping),
                        "provenance": "caller-supplied, unverified by this tool"}
    index, index_notes = build_index(roots, recursive, max_index_files)
    budget = _Budget(max_file_bytes, max_total_bytes)
    cache: Dict[str, Dict] = {}

    def candidate_info(path: str) -> Dict:
        """Hash + machine for a requested candidate only (lazy, capped, cached)."""
        if path not in cache:
            try:
                data = budget.read(path)
                info = {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
                try:
                    info["machine"] = read_machine(data)
                except PEFormatError as exc:
                    info["header_error"] = str(exc)
            except (OSError, PEFormatError) as exc:
                info = {"error": "%s: %s" % (exc.__class__.__name__, exc)}
            cache[path] = info
        return cache[path]

    def resolve(requested: str, importer_machine: int) -> Dict:
        edge = {"requested_name": requested, "classification": None, "resolved": None,
                "candidates": None, "mapped_host": None, "note": None}
        name = requested
        if is_apiset_name(name):
            key = name.lower()
            host = mapping.get(key) if mapping is not None else None
            if host is None or is_apiset_name(host):
                edge["classification"] = "unresolved-api-set"
                edge["note"] = ("API-set contract name; left unresolved. Host mapping needs a "
                                "separately captured runner mapping (schema %s)." % apiset.SCHEMA)
                return edge
            edge["mapped_host"] = host
            name = host
        cands = index.get(name.lower(), [])
        if not cands:
            edge["classification"] = "unresolved-not-present"
            edge["note"] = ("Not present in the declared roots; not evidence the DLL is absent "
                            "on the target OS.")
            return edge
        infos = [(c, candidate_info(c["path"])) for c in cands]
        bad = [(c, i) for c, i in infos if "error" in i]
        if bad:
            edge["classification"] = "candidate-unreadable"
            edge["candidates"] = [{"path": c["path"], "root_label": c["root_label"],
                                   "error": i.get("error"), "sha256": i.get("sha256")}
                                  for c, i in infos]
            edge["note"] = "At least one same-name candidate could not be read within bounds."
            return edge
        if len({i["sha256"] for _, i in infos}) > 1:
            edge["classification"] = "ambiguous"
            edge["candidates"] = [{"path": c["path"], "sha256": i["sha256"],
                                   "root_label": c["root_label"]} for c, i in infos]
            edge["note"] = "Distinct same-name files across declared roots; no selection implied."
            return edge
        chosen, info = infos[0]
        resolved = {"path": chosen["path"], "sha256": info["sha256"],
                    "root_label": chosen["root_label"], "machine": info.get("machine"),
                    "machine_name": None,
                    "duplicates": [c["path"] for c, _ in infos[1:]]}
        edge["resolved"] = resolved
        if "header_error" in info:
            edge["classification"] = "parse-error"
            edge["note"] = "Candidate failed header parse: %s" % info["header_error"]
            return edge
        resolved["machine_name"] = MACHINE_NAMES.get(info["machine"], "0x%04X" % info["machine"])
        if info["machine"] != importer_machine:
            edge["classification"] = "arch-mismatch"
            edge["note"] = ("Candidate machine %s differs from importer; not walked."
                            % resolved["machine_name"])
            return edge
        if edge["mapped_host"]:
            edge["classification"] = "resolved-via-supplied-mapping"
            edge["note"] = ("Same-named file in declared roots for an UNVERIFIED supplied "
                            "mapping; not native API-set resolution.")
        else:
            edge["classification"] = "resolved"
        return edge

    root_norms = [_norm(r["path"]) for r in roots]
    modules: Dict[str, Dict] = {}
    edges: List[Dict] = []
    errors: List[Dict] = []
    visited = set()
    queue = deque()
    for seed in sorted((_norm(s) for s in seeds), key=str.lower):
        if not any(seed.startswith(rn + os.sep) for rn in root_norms):
            raise ValueError("seed %r is not inside any declared root" % seed)
        queue.append((seed, 0))

    while queue:
        path, depth = queue.popleft()
        if path in visited:
            continue
        visited.add(path)
        if len(visited) > max_nodes:
            errors.append({"path": path, "error": "node bound %d exceeded" % max_nodes})
            break
        if depth > max_depth:
            errors.append({"path": path, "error": "depth bound %d exceeded" % max_depth})
            continue
        try:
            data = budget.read(path)
            image = parse_pe(data)
        except (OSError, PEFormatError) as exc:
            errors.append({"path": path, "error": str(exc)})
            continue
        sha = hashlib.sha256(data).hexdigest()
        prior = cache.get(path)
        if prior and prior.get("sha256") not in (None, sha):
            errors.append({"path": path, "error": "file content changed during analysis"})
            continue
        modules[path] = {"path": path, "sha256": sha, "size_bytes": len(data),
                         "machine": image.machine, "machine_name": image.machine_name,
                         "is_seed": depth == 0, "export_dll_name": image.export_dll_name}
        base = os.path.basename(path)
        deps: List[Tuple[str, str, str, Optional[str]]] = []
        for m in image.imports:
            deps.append(("import", m.dll_name, base, None))
        for m in image.delay_imports:
            deps.append(("delay-import", m.dll_name, base, None))
        for exp in image.exports:
            if exp.forwarder:
                label = exp.name if exp.name else "#%d" % exp.ordinal
                deps.append(("export-forwarder", exp.forwarder, "%s!%s" % (base, label),
                             exp.forwarder))
        for kind, requested, importer, fwd in sorted(deps, key=lambda d: (d[0], d[1].lower(), d[2])):
            if kind == "export-forwarder":
                target = forwarder_target(requested)
                if target is None:
                    edge = {"requested_name": requested, "classification": "unparseable-forwarder",
                            "resolved": None, "candidates": None, "mapped_host": None,
                            "note": "Forwarder string not 'Module.Export' with an extensionless "
                                    "module; not interpreted or truncated."}
                else:
                    edge = resolve(target, image.machine)
                edge["forwarder"] = fwd
                edge["over_approximation"] = True
            else:
                edge = resolve(requested, image.machine)
                edge["over_approximation"] = False
            edge["importer"] = importer
            edge["importer_path"] = path
            edge["import_kind"] = kind
            edges.append(edge)
            if edge["classification"] in ("resolved", "resolved-via-supplied-mapping"):
                target_path = _norm(edge["resolved"]["path"])
                if target_path not in visited:
                    queue.append((target_path, depth + 1))

    edges.sort(key=lambda e: (e["importer_path"].lower(), e["import_kind"],
                              e["requested_name"].lower(), e["importer"].lower()))
    summary = {"modules": len(modules), "edges": len(edges)}
    for cls in ("resolved", "resolved-via-supplied-mapping", "unresolved-api-set",
                "unresolved-not-present", "ambiguous", "arch-mismatch", "parse-error",
                "candidate-unreadable", "unparseable-forwarder"):
        summary[cls] = sum(1 for e in edges if e["classification"] == cls)
    summary["errors"] = len(errors)
    summary["index_notes"] = len(index_notes)
    return {
        "schema": REPORT_SCHEMA, "notice": NOTICE,
        "roots": [{"label": r["label"], "path": r["path"]} for r in roots],
        "recursive_index": recursive,
        "seeds": sorted(seeds, key=str.lower),
        "apiset_mapping": mapping_info,
        "limits": {"max_nodes": max_nodes, "max_depth": max_depth,
                   "max_index_files": max_index_files, "max_file_bytes": max_file_bytes,
                   "max_total_bytes": max_total_bytes, "bytes_read": budget.used},
        "index_notes": index_notes,
        "modules": [modules[k] for k in sorted(modules, key=str.lower)],
        "edges": edges, "errors": errors, "summary": summary,
    }


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="peinspect.closure",
                                     description="Deterministic read-only PE closure walker.")
    parser.add_argument("--root", action="append", required=True, metavar="LABEL=PATH")
    parser.add_argument("--seed", action="append", required=True, metavar="PATH")
    parser.add_argument("--recursive-index", action="store_true",
                        help="Opt in to bounded recursive indexing of roots.")
    parser.add_argument("--apiset-mapping", metavar="PATH")
    parser.add_argument("--out", metavar="PATH")
    args = parser.parse_args(argv)
    roots = []
    for spec in args.root:
        if "=" not in spec:
            parser.error("--root must be LABEL=PATH")
        label, path = spec.split("=", 1)
        roots.append({"label": label, "path": os.path.abspath(path)})
    doc = None
    if args.apiset_mapping:
        # size-capped read, then validated again inside run_closure
        apiset.load_mapping(args.apiset_mapping)
        with open(args.apiset_mapping, "rb") as fh:
            doc = json.loads(fh.read(apiset.MAX_MAPPING_BYTES + 1).decode("utf-8"))
    report = run_closure([os.path.abspath(s) for s in args.seed], roots, mapping_doc=doc,
                         recursive=args.recursive_index)
    text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
