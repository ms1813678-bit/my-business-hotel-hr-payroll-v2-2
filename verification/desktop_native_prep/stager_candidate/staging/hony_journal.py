"""Install journal reader/writer and PURE rollback PLANNER (Bv3 NEW fixed candidate, plan format v2).

UNVERIFIED against the real repo. Rebuilt from recovered requirements and the grounded
consumer contracts; not byte-parity, not an exact-parity claim. Every conclusion here is
a hypothesis until checked against the real repository and on Windows.

Changes vs the previous candidate (the journal WIRE format stays schema 1 because the
recovered, unchanged Pascal writes schema 1; the plan DOCUMENT format is v2):

  1. Per-step lifecycle state machine (validate_lifecycle). One start, one terminal
     result, no success before start, no contradictory success/failure, no
     duplicate/retry ambiguity. A step that violates the sequence is TAINTED and
     produces NO removal proposals. A step-failed with no preceding step-start is
     recorded as "failed, not proven" (a failure cannot fabricate success) and does not
     taint other steps; any later lifecycle event for that step taints it.
     Confirmed counterexample now covered by a test: a step-ok before its step-start,
     followed by step-start + step-failed, must NOT leave a vendor-uninstall plan
     (the old planner keyed on a global ok set and lost event order).
  2. Explicit output canonicalization (canon_path) and a stricter Windows path policy:
     refuses trailing dot/space components, ADS (a colon inside any component),
     reserved device names, drive-relative and UNC forms, malformed roots, and
     anchor-kind mismatches between path and install root. Planned removal paths are
     emitted in the JOURNALED form (the `created` record), not a rebuilt placeholder
     expansion, so a slash-form journal path is reported verbatim.
     This is a LEXICAL policy only: it cannot disprove junction, reparse-point or
     8.3 short-name identity. That limit stays unresolved on purpose (PATH_IDENTITY_LIMIT)
     and a lexical path is never represented as a native identity.
  3. Ownership stays report-only: conflicting ownership events (or journal/state-file
     disagreement) downgrade to "unknown" and STAY unknown even after a single later
     reassertion; "unknown" never authorizes a removal proposal.

Posture (unchanged, report-only):
  * This module plans and reports. It executes nothing: no deletion, no vendor
    uninstall, no subprocess. `execute_rollback` refuses every action.
  * It does NOT compensate or restore destroyed data. Protected paths
    (.env, var, certificates, keys) are never planned for removal.
  * Unknown/corrupt/conflicting state fails closed: no removal proposals.
  * Hypothesis: the final product still needs a reviewed native ownership helper
    (exclusive identity, reparse/junction protection, Windows fault injection).
    Nothing here substitutes for that.
"""
import json
import ntpath
import os
import sys

SCHEMA = 1  # journal wire format, written by the recovered (unchanged) Pascal
PLAN_FORMAT = "hony-rollback-plan/v2"  # NEW: versioned plan document format (not recovered)
OURS = "installed-by-this-installer"
PRE = "pre-existing"
SHARED_PRE = "shared-pre-existing"
UNKNOWN = "unknown"
OWNERSHIP_VALUES = {OURS, PRE, SHARED_PRE, UNKNOWN}
COMPONENTS = ("python", "webview2", "postgresql")
# never removed by any plan, whatever the journal says
NEVER_REMOVE_COMPONENTS = {"webview2", "postgresql"}
PROTECTED_NAMES = {".env", "var", "certificates", "keys"}

JOURNAL_EVENTS = {
    "journal-open", "component-state", "ownership", "step-start", "created",
    "step-ok", "step-failed", "preflight-failed", "rollback-start",
    "rollback-left-behind", "rollback-done", "install-complete",
}
NOTE = ("Non-operational planning document. Executes nothing, removes nothing, "
        "does not restore data. Not whole-system atomic.")
PATH_IDENTITY_LIMIT = ("lexical-only: a string policy cannot disprove junction, "
                       "reparse-point or 8.3 short-name identity; a lexical path is "
                       "never a native identity. Unresolved by design.")

# per-step lifecycle states
ST_UNSTARTED = "unstarted"
ST_STARTED = "started"
ST_OK = "ok"
ST_FAILED = "failed"
ST_TAINTED = "tainted"

DEVICE_NAMES = ({"CON", "PRN", "AUX", "NUL"}
                | {"COM%d" % i for i in range(1, 10)}
                | {"LPT%d" % i for i in range(1, 10)})


class JournalError(Exception):
    pass


# ------------------------------------------------------------------ journal io
def parse_journal_text(text):
    """Strict parse. Any anomaly raises JournalError (fail closed)."""
    if not text or not text.strip():
        raise JournalError("journal is empty")
    if not text.endswith("\n"):
        raise JournalError("journal does not end with a newline (truncated last line?)")
    events = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            raise JournalError(f"line {n}: blank line")
        try:
            obj = json.loads(line)
        except ValueError as e:
            raise JournalError(f"line {n}: not valid JSON ({e})")
        if not isinstance(obj, dict):
            raise JournalError(f"line {n}: not an object")
        if obj.get("schema") != SCHEMA:
            raise JournalError(f"line {n}: unsupported schema {obj.get('schema')!r}")
        ev = obj.get("event")
        if ev not in JOURNAL_EVENTS:
            raise JournalError(f"line {n}: unknown event {ev!r}")
        events.append(obj)
    if events[0]["event"] != "journal-open":
        raise JournalError("first event is not journal-open")
    if sum(1 for e in events if e["event"] == "journal-open") != 1:
        raise JournalError("journal-open appears more than once")
    return events


def read_journal(path):
    try:
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read().replace("\r\n", "\n")
    except OSError as e:
        raise JournalError(f"journal unreadable: {e.__class__.__name__}")
    except UnicodeDecodeError:
        raise JournalError("journal is not valid UTF-8")
    return parse_journal_text(text)


def append_event(journal_path, event, **fields):
    """Append one event and fsync. Used by tests and tooling; creates only the journal file."""
    if event not in JOURNAL_EVENTS:
        raise JournalError(f"unknown event {event!r}")
    line = json.dumps({"schema": SCHEMA, "event": event, **fields}, sort_keys=True) + "\n"
    with open(journal_path, "a", encoding="utf-8", newline="") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


# ---------------------------------------------------------- lifecycle validation
def validate_lifecycle(events):
    """NEW: per-step sequence validation, walked in journal order.

    Returns (states, violations, notes):
      states     {step id: one of unstarted/started/ok/failed/tainted}
      violations list of {"id", "detail"} for sequence violations; each taints its step
      notes      list of {"id", "detail"} for anomalies that do NOT taint

    Rules (one start, one terminal result):
      * step-start is valid only from unstarted; a later start taints (duplicate/retry
        ambiguity).
      * step-ok is valid only from started; ok before start, ok after a terminal, or a
        second ok taints (success-before-start / contradictory / duplicate).
      * step-failed is valid from started; with no prior start it records "failed"
        (a failure cannot fabricate success) as a NOTE; failed after a terminal taints
        (contradictory outcome); any later lifecycle event for a failed step taints it.
      * A tainted step is never eligible for removal proposals.
    """
    states, violations, notes = {}, [], []

    def taint(sid, detail):
        states[sid] = ST_TAINTED
        violations.append({"id": sid, "detail": detail})

    for e in events:
        ev = e["event"]
        if ev not in ("step-start", "step-ok", "step-failed"):
            continue
        sid = e.get("id")
        if not isinstance(sid, str) or not sid:
            taint(sid if isinstance(sid, str) else "", f"{ev} without a usable id")
            continue
        cur = states.get(sid, ST_UNSTARTED)
        if cur == ST_TAINTED:
            violations.append({"id": sid, "detail": f"{ev} after an earlier lifecycle violation"})
            continue
        if ev == "step-start":
            if cur == ST_UNSTARTED:
                states[sid] = ST_STARTED
            else:
                taint(sid, f"duplicate step-start (state {cur}): retry ambiguity")
        elif ev == "step-ok":
            if cur == ST_STARTED:
                states[sid] = ST_OK
            elif cur == ST_UNSTARTED:
                taint(sid, "step-ok before step-start: success claimed before the step began")
            else:
                taint(sid, f"step-ok after terminal {cur}: contradictory/duplicate outcome")
        else:  # step-failed
            if cur == ST_STARTED:
                states[sid] = ST_FAILED
            elif cur == ST_UNSTARTED:
                states[sid] = ST_FAILED
                notes.append({"id": sid, "detail": "step-failed with no preceding "
                                                   "step-start (recorded failed, not proven)"})
            else:
                taint(sid, f"step-failed after terminal {cur}: contradictory outcome")
    return states, violations, notes


# ------------------------------------------------------------------ path policy
def _split(path):
    """Validate anchor and components; return (kind, drive, components). Lexical only.

    kind is "drive" (C:\\...) or "rooted" (\\... on the current drive). Raises
    ValueError with the refusal reason for anything else or for any refused component.
    """
    if not isinstance(path, str) or not path.strip():
        raise ValueError("empty path")
    if any(ord(c) < 32 for c in path):
        raise ValueError("control character in path")
    p = path.replace("/", "\\")
    drive = None
    if len(p) >= 2 and p[0].isalpha() and p[1] == ":":
        drive = p[0].lower()
        rest = p[2:]
        if not rest.startswith("\\"):
            raise ValueError("drive-relative path")
        rest = rest[1:]
        if rest.startswith("\\"):
            raise ValueError("malformed root")
        kind = "drive"
    elif p.startswith("\\\\"):
        raise ValueError("UNC path")
    elif p.startswith("\\"):
        rest = p[1:]
        if not rest.startswith("\\"):
            kind = "rooted"
        else:
            raise ValueError("malformed root")
    else:
        raise ValueError("relative path")
    comps = rest.split("\\")
    for c in comps:
        if not c:
            raise ValueError("malformed path (empty component)")
        if c == "..":
            raise ValueError("parent-directory component")
        if c.endswith(".") or c.endswith(" "):
            raise ValueError("trailing dot or space in component")
        if ":" in c:
            raise ValueError("alternate data stream (colon in component)")
        stem = c.split(".", 1)[0].upper()
        if stem in DEVICE_NAMES:
            raise ValueError("reserved device name")
    return kind, drive, comps


def canon_path(path):
    """NEW explicit output canonicalization. Windows semantics, lexical only:
    '/' and '\\' are both separators (normalized to '\\'), case-folded via normcase,
    then normpath. Components were validated by _split first, so normpath cannot
    silently collapse anything meaningful. Never a native identity (PATH_IDENTITY_LIMIT).
    """
    kind, drive, comps = _split(path)
    base = (drive + ":\\") if kind == "drive" else "\\"
    return ntpath.normcase(ntpath.normpath(base + "\\".join(comps)))


def _canon_eq(a, b):
    try:
        return canon_path(a) == canon_path(b)
    except ValueError:
        return False


def resolve_placeholders(path, placeholders):
    out = path
    for k, v in (placeholders or {}).items():
        out = out.replace(k, v)
    return out


def path_refusal(path, app_root):
    """Return a reason string if `path` must never be planned for removal, else None."""
    if not isinstance(path, str) or not path.strip():
        return "empty path"
    if "{" in path or "}" in path:
        return "unresolved placeholder"
    try:
        pkind, pdrive, _ = _split(path)
    except ValueError as e:
        return str(e)
    if not app_root or "{" in app_root:
        return "install root unknown"
    try:
        rkind, rdrive, _ = _split(app_root)
    except ValueError as e:
        return "malformed install root: " + str(e)
    if pkind != rkind:
        return "anchor kind differs from install root"
    if pdrive != rdrive:
        return "different drive than install root"
    n, root = canon_path(path), canon_path(app_root)
    if n == root:
        return "install root itself"
    if not n.startswith(root + "\\"):
        return "outside install root"
    rel = n[len(root) + 1:].split("\\")
    if any(part in PROTECTED_NAMES for part in rel):
        return "protected path (.env, var, certificates, keys)"
    return None


# ------------------------------------------------------------------ planner
def _step_effects(plan, step_id):
    for s in plan.get("steps", []):
        if s.get("id") == step_id:
            return s.get("effects", [])
    return None


def plan_rollback(plan, events, ownership=None, placeholders=None):
    """Pure. Walk the journal backwards and describe what a reviewed helper could do.

    ownership: optional dict {component: value} from a state file. The journal's own
    'ownership' events win; a conflict is reported and downgraded to unknown, and
    unknown STAYS unknown even after a single later reassertion. Ownership is a
    report-only observation: it never grants removal authority by itself.
    """
    placeholders = placeholders or {}
    app_root = placeholders.get("{app}", "")
    own = {c: UNKNOWN for c in COMPONENTS}
    left, actions = [], []
    for c, v in (ownership or {}).items():
        own[c] = v if v in OWNERSHIP_VALUES else UNKNOWN
    journal_own = {}
    started, created = [], []
    step_states, violations, notes = validate_lifecycle(events)
    # NEW: a `created` record is evidence only when it falls inside the interval of its own
    # step: after that step's step-start and before any terminal result. Walked in order.
    live = {}
    outside = []
    for e in events:
        ev = e["event"]
        sid = e.get("id") if ev in ("step-start", "step-ok", "step-failed") else e.get("step")
        if ev == "step-start":
            live[sid] = "open" if sid not in live else "closed"
        elif ev in ("step-ok", "step-failed"):
            if live.get(sid) == "open":
                live[sid] = "closed"
        elif ev == "created":
            if live.get(sid) == "open" and isinstance(e.get("path"), str):
                created.append(e)
            else:
                outside.append(e)
    for e in events:
        ev = e["event"]
        if ev == "ownership":
            c, v = e.get("component"), e.get("ownership")
            if c not in COMPONENTS or v not in OWNERSHIP_VALUES:
                left.append({"effect": {"component": c}, "reason": "malformed ownership event"})
                continue
            if c in journal_own and journal_own[c] != v:
                journal_own[c] = UNKNOWN  # conflict; a later reassertion stays unknown
            else:
                journal_own[c] = v
        elif ev == "step-start":
            if e.get("id") not in started:
                started.append(e.get("id"))
    for c, v in journal_own.items():
        if own[c] not in (UNKNOWN, v):
            own[c] = UNKNOWN
            left.append({"effect": {"component": c}, "reason": "journal and state file disagree on ownership"})
        else:
            own[c] = v
    for e in outside:
        left.append({"effect": {"step": e.get("step"), "path": e.get("path")},
                     "reason": "created record outside its step's start..result interval: not evidence"})
    created_paths = {}
    for e in created:
        created_paths.setdefault(e.get("step"), []).append(e)
    for sid in reversed(started):
        effects = _step_effects(plan, sid)
        if effects is None:
            left.append({"effect": {"step": sid}, "reason": "step not in install plan"})
            continue
        if step_states.get(sid) == ST_TAINTED:
            for eff in effects:
                left.append({"effect": eff, "step": sid,
                             "reason": "lifecycle violation: step state not trusted; no removal proposals"})
            continue
        proven = step_states.get(sid) == ST_OK
        if not proven:
            state = step_states.get(sid, ST_UNSTARTED)
            detail = (" (step-failed)" if state == ST_FAILED else " (no outcome recorded)")
            for eff in effects:
                left.append({"effect": eff, "step": sid, "reason": "no step-ok: outcome not proven" + detail})
            continue
        for eff in effects:
            kind = eff.get("kind")
            if kind == "runtime":
                c = eff.get("component")
                if c in NEVER_REMOVE_COMPONENTS:
                    left.append({"effect": eff, "step": sid, "reason": "never removed by policy (shared runtime or data-bearing service)"})
                elif own.get(c) != OURS:
                    left.append({"effect": eff, "step": sid, "reason": f"ownership {own.get(c, UNKNOWN)}: not removed"})
                elif not eff.get("reversible", False):
                    left.append({"effect": eff, "step": sid, "reason": "not reversible by this installer"})
                else:
                    actions.append({"kind": "vendor-uninstall", "component": c, "step": sid,
                                    "report_only": True,
                                    "requires": ["ownership", "step-ok", "install-dir-scoped uninstall (UNVERIFIED)"]})
            elif kind == "dir":
                resolved = resolve_placeholders(eff.get("path", ""), placeholders)
                recs = [r for r in created_paths.get(sid, [])
                        if r.get("kind") == "dir" and _canon_eq(r.get("path", ""), resolved)]
                if len(recs) > 1:
                    left.append({"effect": eff, "step": sid,
                                 "reason": "ambiguous: multiple created records for this path in the step interval"})
                    continue
                if not recs:
                    malformed = []
                    for r in created_paths.get(sid, []):
                        try:
                            _split(r.get("path", ""))
                        except ValueError as e:
                            malformed.append((r, str(e)))
                    if malformed:  # a malformed journaled path is refused loudly, not skipped
                        for r, why in malformed:
                            left.append({"effect": {**eff, "path": r.get("path")}, "step": sid,
                                         "reason": "refused: " + why})
                    else:
                        left.append({"effect": eff, "step": sid, "reason": "no created record in journal"})
                    continue
                journaled = recs[0].get("path", "")  # emit the journaled form verbatim
                why = path_refusal(journaled, app_root)
                if why:
                    left.append({"effect": {**eff, "path": journaled}, "step": sid, "reason": "refused: " + why})
                else:
                    actions.append({"kind": "remove-path", "path": journaled, "step": sid,
                                    "report_only": True,
                                    "requires": ["identity check", "no reparse points (UNVERIFIED)"]})
            elif kind == "inside-created-dir":
                pass  # covered by the directory effect of its creating step
            else:
                left.append({"effect": eff, "step": sid, "reason": "not reversible by this installer"})
    return {
        "schema": SCHEMA,
        "format": PLAN_FORMAT,
        "operational": False,
        "note": NOTE,
        "atomicity": "none",
        "refused": False,
        "path_identity": PATH_IDENTITY_LIMIT,
        "lifecycle_valid": not violations,
        "lifecycle_violations": violations,
        "lifecycle_notes": notes,
        "ownership": own,
        "actions": actions,
        "left_behind": left,
        "complete": any(e["event"] == "install-complete" for e in events),
    }


def refused_plan(reason):
    return {"schema": SCHEMA, "format": PLAN_FORMAT, "operational": False, "note": NOTE,
            "path_identity": PATH_IDENTITY_LIMIT,
            "refused": True, "reason": reason, "actions": [], "left_behind": []}


def execute_rollback(actions, runner=None):
    """Refuses every action, under any input. `runner` is ignored and never called."""
    return {"refused": True, "operational": False, "executed": [],
            "reason": "this scaffold executes nothing; a reviewed native helper is required",
            "actions_seen": len(list(actions or []))}


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="hony_journal")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan-rollback")
    p.add_argument("--journal", required=True)
    p.add_argument("--plan", required=True)
    p.add_argument("--install-root", default="")
    p.add_argument("--ownership-state")
    p.add_argument("--execute", action="store_true")
    a = ap.parse_args(argv)
    if a.execute:
        print(json.dumps(execute_rollback([]), indent=2))
        return 2
    try:
        events = read_journal(a.journal)
        with open(a.plan, "r", encoding="utf-8") as f:
            plan = json.load(f)
        own = None
        if a.ownership_state:
            with open(a.ownership_state, "r", encoding="utf-8") as f:
                own = {k: v.get("ownership") for k, v in json.load(f)["components"].items()}
    except (JournalError, OSError, ValueError, KeyError, AttributeError) as e:
        print(json.dumps(refused_plan(f"{e.__class__.__name__}: {e}"), indent=2))
        return 3
    print(json.dumps(plan_rollback(plan, events, own, {"{app}": a.install_root}), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
