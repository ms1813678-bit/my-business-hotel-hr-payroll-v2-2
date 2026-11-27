"""The scenarios from docs/SYNC_DESIGN.md section 5, run against real processes.

Every scenario gets three freshly migrated PostgreSQL databases (main, device B,
device C), three real processes, and asserts the final state of all three. The
seed data is synthetic: two departments, three employees, one leave type and one
branch, with fixed sync_uids so a scenario can name a record.
"""

from __future__ import annotations

import datetime as dt
import json
import random
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app import sync_visibility

from . import contract, invariants, provision, store
from .client import HttpError, post_json
from .supervisor import InstanceSpec, Supervisor

ROLE_ADMIN = sync_visibility.ROLE_ADMIN
ROLE_ASSISTANT = sync_visibility.ROLE_ASSISTANT
ROLE_FINANCE = sync_visibility.ROLE_FINANCE

DEPT_OPS = "11111111-1111-4111-8111-111111111111"
DEPT_FIN = "22222222-2222-4222-8222-222222222222"
E1 = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
E2 = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
E3 = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
LEAVE_TYPE = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"

ROLES = {"A": ROLE_ADMIN, "B": ROLE_ASSISTANT, "C": ROLE_FINANCE}


# ---------------------------------------------------------------- seed


def seed_main(engine: Engine) -> None:
    """Synthetic base data, written straight into the main device's database."""
    created = "2026-11-01T09:00:00+00:00"
    with engine.begin() as connection:
        store.insert_row(connection, "departments", DEPT_OPS,
                         {"name": "Operations", "active": True}, role=ROLE_ADMIN)
        store.insert_row(connection, "departments", DEPT_FIN,
                         {"name": "Finance", "active": True}, role=ROLE_ADMIN)
        connection.execute(text("INSERT INTO branches(department_id, name, active) "
                                "SELECT id, 'Main Branch', true FROM departments WHERE sync_uid = :uid"),
                           {"uid": DEPT_OPS})
        store.insert_row(connection, "employees", E1, {
            "code": "EMP-0001", "full_name": "Seed One", "national_id": "29001010101010",
            "nationality": "egyptian", "phone": "01000000001", "address": "Seed address one",
            "department_uid": DEPT_OPS, "position": "Clerk", "hire_date": "2024-01-01",
            "status": "active", "basic_salary": "5000.00", "daily_hours": "8",
            "created_at": created,
        }, role=ROLE_ADMIN)
        connection.execute(text("UPDATE employees SET branch_id = (SELECT id FROM branches LIMIT 1) "
                                "WHERE sync_uid = :u"), {"u": E1})
        store.insert_row(connection, "employees", E2, {
            "code": "EMP-0002", "full_name": "Seed Two", "national_id": "29001010101011",
            "nationality": "egyptian", "phone": "01000000002", "address": "Seed address two",
            "department_uid": DEPT_FIN, "position": "Accountant", "hire_date": "2023-05-01",
            "status": "active", "basic_salary": "7000.00", "daily_hours": "8",
            "created_at": created,
        }, role=ROLE_ADMIN)
        store.insert_row(connection, "employees", E3, {
            "code": "EMP-0003", "full_name": "Seed Three", "nationality": "egyptian",
            "phone": "01000000003", "address": "Seed address three", "department_uid": DEPT_OPS,
            "position": "Driver", "hire_date": "2025-02-01", "status": "active",
            "basic_salary": "4000.00", "daily_hours": "8", "created_at": created,
        }, role=ROLE_ADMIN)
        store.insert_row(connection, "leave_types", LEAVE_TYPE, {
            "code": "ANNUAL", "name_ar": "سنوي", "name_en": "Annual", "is_paid": True,
            "tracks_balance": True, "active": True,
        }, role=ROLE_ADMIN)


# ---------------------------------------------------------------- context


class Context:
    """One scenario's world: three databases, three processes, one engine each."""

    def __init__(self, provisioner: provision.Provisioner, work_dir: Path, *, engine: str,
                 seed: int):
        self.provisioner = provisioner
        self.work_dir = Path(work_dir)
        self.engine_kind = engine
        self.seed = seed
        self.supervisor: Supervisor | None = None
        self.engines: dict[str, Engine] = {}
        self.notes: dict = {}

    def start(self) -> None:
        self.provisioner.reset()
        urls = self.provisioner.urls()
        specs = [
            InstanceSpec("A", "main", "main", ROLE_ADMIN, urls["A"]),
            InstanceSpec("B", "device", "device-b", ROLE_ASSISTANT, urls["B"]),
            InstanceSpec("C", "device", "device-c", ROLE_FINANCE, urls["C"]),
        ]
        self.supervisor = Supervisor(specs, self.work_dir, engine=self.engine_kind)
        self.engines = {name: store.make_engine(urls[name]) for name in ("A", "B", "C")}
        # Finding: migrations seed reference rows (for example leave type "annual")
        # with a different random sync_uid in every database, so two installs
        # disagree about which row is which. The harness starts from empty
        # reference tables and lets the main device's seed define them.
        for name in ("A", "B", "C"):
            with self.engines[name].begin() as connection:
                for table in ("leave_records", "leave_types"):
                    connection.execute(text(f"DELETE FROM {table}"))
        seed_main(self.engines["A"])
        self.supervisor.start_all()
        main = self.client("A")
        main.pair("device-b", ROLE_ASSISTANT)
        main.pair("device-c", ROLE_FINANCE)

    def reset(self) -> None:
        self.close()
        self.start()

    def close(self) -> None:
        if self.supervisor is not None:
            self.supervisor.stop_all()
        for engine in self.engines.values():
            engine.dispose()
        self.engines = {}

    # -- accessors

    def client(self, name: str):
        assert self.supervisor is not None
        return self.supervisor.client(name)

    def engine(self, name: str) -> Engine:
        return self.engines[name]

    @property
    def main_url(self) -> str:
        assert self.supervisor is not None
        return self.supervisor.main_url

    def value(self, name: str, role: str, entity: str, uid: str, field: str):
        return invariants.projection(self.engine(name), role).get((entity, uid), {}).get(field)

    def state(self, name: str = "A") -> dict:
        return self.client(name).state()

    def pending_conflicts(self) -> list[dict]:
        return invariants.pending_conflicts(self.engine("A"))

    def conflicts(self) -> list[dict]:
        with self.engine("A").connect() as connection:
            return [dict(row) for row in connection.execute(
                text("SELECT * FROM harness_sync_conflict ORDER BY id")).mappings()]


# ---------------------------------------------------------------- checks


@dataclass
class Checks:
    items: list[dict] = field(default_factory=list)

    def check(self, name: str, ok, detail: str = "") -> bool:
        self.items.append({"check": name, "ok": bool(ok), "detail": "" if ok else str(detail)[:800]})
        return bool(ok)

    def eq(self, name: str, actual, expected) -> bool:
        return self.check(name, actual == expected, f"actual={actual!r} expected={expected!r}")

    def failures(self) -> list[dict]:
        return [item for item in self.items if not item["ok"]]


# ---------------------------------------------------------------- helpers


def sync(ctx: Context, name: str) -> dict:
    assert ctx.supervisor is not None
    return ctx.client(name).sync(ctx.main_url)


def converge(ctx: Context, rounds: int = 2, names=("B", "C")) -> None:
    for _ in range(rounds):
        for name in names:
            sync(ctx, name)


def snapshot_all(ctx: Context) -> dict:
    with ctx.engine("A").connect() as connection:
        counter = store.current_counter(connection)
        log_rows = connection.execute(text("SELECT count(*) FROM harness_sync_change")).scalar()
    return {
        "A": invariants.content_hash(ctx.engine("A"), ROLE_ADMIN),
        "B": invariants.content_hash(ctx.engine("B"), ROLE_ASSISTANT),
        "C": invariants.content_hash(ctx.engine("C"), ROLE_FINANCE),
        "counter": counter,
        "log_rows": log_rows,
    }


def assert_converged(ctx: Context, checks: Checks, *, label: str) -> None:
    for name in ("B", "C"):
        problems = invariants.differences(ctx.engine("A"), ctx.engine(name), ROLES[name])
        checks.check(f"{label}: main and device {name} agree", not problems, problems[:8])
    checks.eq(f"{label}: row counts agree", invariants.row_counts(ctx.engine("A")),
              invariants.row_counts(ctx.engine("B")))
    checks.eq(f"{label}: row counts agree (C)", invariants.row_counts(ctx.engine("A")),
              invariants.row_counts(ctx.engine("C")))


def raw_session(ctx: Context, *, device_id: str, role: str, changes: list[dict],
                base_counter: int = 0) -> dict:
    """Send a hand-built session straight to the main device (a forged device)."""
    epoch = ctx.client("A").health()["epoch"]
    request = {"protocol": contract.PROTOCOL, "session_id": str(uuid.uuid4()),
               "device_id": device_id, "role": role, "epoch": epoch,
               "base_counter": base_counter, "changes": changes}
    return post_json(ctx.main_url + "/sync/session", request)


def forged_changes(device_id: str, entries: list[tuple], *, start_seq: int = 1,
                   prev: str = "") -> list[dict]:
    """entries: (op, entity, uid, field, old, new) -> a hash-chained change list."""
    changes = []
    for index, (op, entity, uid, field, old, new) in enumerate(entries, start=start_seq):
        change = contract.make_change(
            prev, device_id=device_id, seq=index, op=op, entity=entity, uid=uid, field=field,
            old=old, new=new, local_time=dt.datetime.now(dt.timezone.utc).isoformat())
        changes.append(change)
        prev = change["hash"]
    return changes


def _head_of(ctx: Context, device_id: str) -> str:
    with ctx.engine("A").connect() as connection:
        return connection.execute(text(
            "SELECT last_hash FROM harness_sync_device WHERE device_id = :d"), {"d": device_id}).scalar() or ""


def bump_restore_epoch(engine: Engine) -> str:
    """What the product's restore finalization does: a new epoch after the restore."""
    epoch = uuid.uuid4().hex
    with engine.begin() as connection:
        connection.execute(
            text("""INSERT INTO system_settings(key, value, updated_by, updated_at)
                    VALUES ('restore_epoch', :value, 'restore-harness', now())
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                        updated_by = EXCLUDED.updated_by, updated_at = now()"""),
            {"value": epoch})
    return epoch


# ---------------------------------------------------------------- scenarios


def s_two_way_edits(ctx: Context, checks: Checks) -> None:
    """Different records edited on two devices merge silently (rule 1)."""
    converge(ctx)
    ctx.client("B").local_change(op="set", entity="employees", uid=E1, field="full_name",
                                 value="Name From Assistant")
    ctx.client("C").local_change(op="set", entity="employees", uid=E2, field="position",
                                 value="Senior Accountant")
    b = sync(ctx, "B")
    c = sync(ctx, "C")
    checks.eq("B's change applied", b["applied"], [1])
    checks.eq("C's change applied", c["applied"], [1])
    checks.eq("no conflicts", len(b["conflicts"]) + len(c["conflicts"]), 0)
    converge(ctx)
    checks.eq("main took B's edit", ctx.value("A", ROLE_ADMIN, "employees", E1, "full_name"),
              "Name From Assistant")
    checks.eq("main took C's edit", ctx.value("A", ROLE_ADMIN, "employees", E2, "position"),
              "Senior Accountant")
    checks.check("B never sees C's position edit (hidden from the assistant)",
                 ctx.value("B", ROLE_ASSISTANT, "employees", E2, "position") is None)
    assert_converged(ctx, checks, label="two-way")


def s_different_fields_same_record(ctx: Context, checks: Checks) -> None:
    """Different fields of one record both apply (rule 2)."""
    converge(ctx)
    ctx.client("B").local_change(op="set", entity="employees", uid=E1, field="full_name",
                                 value="Renamed By Assistant")
    ctx.client("C").local_change(op="set", entity="employees", uid=E1, field="basic_salary",
                                 value="6500.00")
    b = sync(ctx, "B")
    c = sync(ctx, "C")
    checks.eq("both changes applied", (b["applied"], c["applied"]), ([1], [1]))
    checks.eq("no conflict on the record", len(b["conflicts"]) + len(c["conflicts"]), 0)
    converge(ctx)
    checks.eq("name kept", ctx.value("A", ROLE_ADMIN, "employees", E1, "full_name"),
              "Renamed By Assistant")
    checks.eq("salary kept", ctx.value("A", ROLE_ADMIN, "employees", E1, "basic_salary"), "6500.00")
    assert_converged(ctx, checks, label="different fields")


def _make_status_conflict(ctx: Context) -> tuple[dict, int]:
    converge(ctx)
    ctx.client("B").local_change(op="set", entity="employees", uid=E1, field="status",
                                 value="on_leave")
    ctx.client("A").local_change(op="set", entity="employees", uid=E1, field="status",
                                 value="suspended")
    summary = sync(ctx, "B")
    conflict = ctx.pending_conflicts()[0]
    return summary, conflict["id"]


def s_same_field_conflict_accept(ctx: Context, checks: Checks) -> None:
    """Same field on two devices is a conflict; accepting the device's value wins (rule 3)."""
    summary, conflict_id = _make_status_conflict(ctx)
    checks.eq("device change held as a conflict", [c["kind"] for c in summary["conflicts"]],
              ["same_field"])
    checks.eq("main value unchanged", ctx.value("A", ROLE_ADMIN, "employees", E1, "status"),
              "suspended")
    conflict = ctx.conflicts()[0]
    checks.eq("conflict shows the device value", json.loads(conflict["device_value"]), "on_leave")
    checks.eq("conflict shows the main value", json.loads(conflict["main_value"]), "suspended")
    checks.eq("conflict names the device", conflict["device_id"], "device-b")
    checks.check("conflict has the device time", conflict["device_time"] is not None)
    checks.check("conflict has the main receive time", conflict["main_time"] is not None)
    checks.eq("conflict is pending", conflict["status"], "pending")

    ctx.client("A").resolve_conflict(conflict_id, "accept")
    checks.eq("accepting applies the device value",
              ctx.value("A", ROLE_ADMIN, "employees", E1, "status"), "on_leave")
    converge(ctx)
    checks.eq("assistant device sees the accepted value",
              ctx.value("B", ROLE_ASSISTANT, "employees", E1, "status"), "on_leave")
    checks.eq("finance device sees the accepted value",
              ctx.value("C", ROLE_FINANCE, "employees", E1, "status"), "on_leave")
    checks.eq("conflict recorded as accepted", ctx.conflicts()[0]["status"], "accepted")
    with ctx.engine("B").connect() as connection:
        checks.eq("device queue cleared", connection.execute(
            text("SELECT state FROM harness_sync_outbox WHERE seq = 1")).scalar(), "applied")
    assert_converged(ctx, checks, label="conflict accepted")


def s_same_field_conflict_reject(ctx: Context, checks: Checks) -> None:
    """Rejecting keeps the main value and puts the device's row back (rule 3)."""
    summary, conflict_id = _make_status_conflict(ctx)
    ctx.client("A").resolve_conflict(conflict_id, "reject")
    second = sync(ctx, "B")
    checks.eq("device is told the outcome", [r["outcome"] for r in second["resolved"]], ["rejected"])
    checks.eq("main value still wins", ctx.value("A", ROLE_ADMIN, "employees", E1, "status"),
              "suspended")
    checks.eq("device row reverted to the main value",
              ctx.value("B", ROLE_ASSISTANT, "employees", E1, "status"), "suspended")
    checks.eq("conflict recorded as rejected", ctx.conflicts()[0]["status"], "rejected")
    converge(ctx)
    assert_converged(ctx, checks, label="conflict rejected")


def s_delete_vs_edit(ctx: Context, checks: Checks) -> None:
    """A delete against an edited record is a conflict, never a silent delete (rule 4)."""
    converge(ctx)
    ctx.client("B").local_change(op="delete", entity="employees", uid=E3)
    ctx.client("A").local_change(op="set", entity="employees", uid=E3, field="status",
                                 value="suspended")
    summary = sync(ctx, "B")
    checks.eq("delete held as a conflict", [c["kind"] for c in summary["conflicts"]],
              ["delete_vs_edit"])
    checks.check("record still exists on the main device",
                 ctx.value("A", ROLE_ADMIN, "employees", E3, "code") == "EMP-0003")
    checks.check("device deleted it locally before the decision",
                 ctx.value("B", ROLE_ASSISTANT, "employees", E3, "code") is None)

    ctx.client("A").resolve_conflict(ctx.conflicts()[0]["id"], "reject")
    sync(ctx, "B")
    checks.eq("rejected delete comes back on the device",
              ctx.value("B", ROLE_ASSISTANT, "employees", E3, "code"), "EMP-0003")
    checks.eq("and it does not carry fields the role may not see",
              "basic_salary" in invariants.projection(ctx.engine("B"), ROLE_ASSISTANT)[("employees", E3)],
              False)
    converge(ctx)
    assert_converged(ctx, checks, label="delete rejected")


def s_edit_vs_edit_delete_accepted(ctx: Context, checks: Checks) -> None:
    """An accepted delete removes the record everywhere, including an applied edit."""
    converge(ctx)
    ctx.client("B").local_change(op="set", entity="employees", uid=E3, field="full_name",
                                 value="Edited Before Delete")
    ctx.client("C").local_change(op="delete", entity="employees", uid=E3)
    ctx.client("A").local_change(op="set", entity="employees", uid=E3, field="position",
                                 value="Night Driver")
    b = sync(ctx, "B")
    c = sync(ctx, "C")
    checks.eq("the edit applied", b["applied"], [1])
    checks.eq("the delete is a conflict", [x["kind"] for x in c["conflicts"]], ["delete_vs_edit"])
    ctx.client("A").resolve_conflict(ctx.conflicts()[0]["id"], "accept")
    converge(ctx)
    for name in ("A", "B", "C"):
        checks.check(f"record gone from {name}",
                     ctx.value(name, ROLES[name], "employees", E3, "code") is None)
    checks.eq("the delete is recorded as accepted", ctx.conflicts()[0]["status"], "accepted")
    assert_converged(ctx, checks, label="delete accepted")


def s_offline_reconnect(ctx: Context, checks: Checks) -> None:
    """The main device is off and a device restarts; both sides converge after reconnect."""
    converge(ctx)
    ctx.supervisor.stop("A")                      # the main device does not have to stay on
    ctx.client("B").local_change(op="set", entity="employees", uid=E1, field="full_name",
                                 value="Edited While Main Was Off")
    ctx.client("C").local_change(op="set", entity="employees", uid=E2, field="position",
                                 value="Edited Offline")
    try:
        sync(ctx, "B")
        checks.check("a device cannot sync while the main device is off", False, "sync succeeded")
    except Exception:
        checks.check("a device cannot sync while the main device is off", True)
    ctx.supervisor.restart("B")                   # a device restart keeps its queue on disk
    with ctx.engine("B").connect() as connection:
        checks.eq("queued edit survived the restart", connection.execute(
            text("SELECT count(*) FROM harness_sync_outbox WHERE state = 'queued'")).scalar(), 1)
    ctx.supervisor.start("A")
    converge(ctx)
    checks.eq("offline main edit reached the devices",
              ctx.value("B", ROLE_ASSISTANT, "employees", E1, "full_name"),
              "Edited While Main Was Off")
    checks.eq("offline device edit reached the main device",
              ctx.value("A", ROLE_ADMIN, "employees", E2, "position"), "Edited Offline")
    assert_converged(ctx, checks, label="offline")


def _interrupted(ctx: Context, checks: Checks, point: str) -> None:
    """Kill one process at one point, retry, and compare with a clean run."""
    target = "A" if point.startswith("main") else "B"

    def run(armed: bool) -> dict:
        converge(ctx)
        if armed:
            ctx.client(target).arm_fault(point)
        ctx.client("B").local_change(op="set", entity="employees", uid=E1, field="full_name",
                                     value="Interrupted Edit")
        ctx.client("B").local_change(op="set", entity="employees", uid=E1, field="status",
                                     value="on_leave")
        if armed:
            try:
                sync(ctx, "B")
            except Exception:
                pass
            code = ctx.supervisor.wait_exit(target)
            checks.eq(f"{point}: the process died at the armed point", code,
                      contract.FAULT_EXIT_CODE)
            ctx.supervisor.restart(target)
        for _ in range(3):
            sync(ctx, "B")
        converge(ctx)
        return snapshot_all(ctx)

    clean = run(False)
    ctx.reset()
    faulted = run(True)
    checks.eq(f"{point}: final state equals a clean run", faulted, clean)
    checks.eq(f"{point}: each change applied exactly once",
              ctx.value("A", ROLE_ADMIN, "employees", E1, "full_name"), "Interrupted Edit")
    with ctx.engine("A").connect() as connection:
        rows = connection.execute(text(
            "SELECT count(*) FROM harness_sync_change WHERE device_id = 'device-b'")).scalar()
    checks.eq(f"{point}: one change log row per device change", rows, 2)


def s_interrupted_before_send(ctx: Context, checks: Checks) -> None:
    _interrupted(ctx, checks, "device_before_send")


def s_interrupted_during_receive(ctx: Context, checks: Checks) -> None:
    _interrupted(ctx, checks, "device_during_receive")


def s_interrupted_before_confirm(ctx: Context, checks: Checks) -> None:
    _interrupted(ctx, checks, "device_before_confirm")


def s_interrupted_device_mid_apply(ctx: Context, checks: Checks) -> None:
    _interrupted(ctx, checks, "device_mid_apply")


def s_interrupted_main_mid_apply(ctx: Context, checks: Checks) -> None:
    _interrupted(ctx, checks, "main_mid_apply")


def s_interrupted_main_before_commit(ctx: Context, checks: Checks) -> None:
    _interrupted(ctx, checks, "main_before_commit")


def s_main_rollback_is_atomic(ctx: Context, checks: Checks) -> None:
    """A main device killed mid-apply leaves no partial rows (rule 6)."""
    converge(ctx)
    ctx.client("A").arm_fault("main_mid_apply")
    ctx.client("B").local_change(op="set", entity="employees", uid=E1, field="full_name",
                                 value="Half Applied")
    ctx.client("B").local_change(op="set", entity="employees", uid=E1, field="status",
                                 value="on_leave")
    ctx.client("B").local_change(op="set", entity="employees", uid=E2, field="full_name",
                                 value="Half Applied Two")
    before = invariants.snapshot(ctx.engine("A"), ROLE_ADMIN)
    with ctx.engine("A").connect() as connection:
        sessions_before = connection.execute(text("SELECT count(*) FROM harness_sync_session")).scalar()
    try:
        sync(ctx, "B")
    except Exception:
        pass
    checks.eq("main died", ctx.supervisor.wait_exit("A"), contract.FAULT_EXIT_CODE)
    invariants.assert_unchanged(before, ctx.engine("A"), ROLE_ADMIN, label="crashed session")
    with ctx.engine("A").connect() as connection:
        checks.eq("no session row survived", connection.execute(
            text("SELECT count(*) FROM harness_sync_session")).scalar(), sessions_before)
    ctx.supervisor.restart("A")
    converge(ctx)
    checks.eq("retry applied everything", ctx.value("A", ROLE_ADMIN, "employees", E2, "full_name"),
              "Half Applied Two")
    assert_converged(ctx, checks, label="after rollback")


def s_duplicate_code(ctx: Context, checks: Checks) -> None:
    """Two offline devices create the same employee code; main hands out the next (rule 7)."""
    converge(ctx)
    uid_b = str(uuid.uuid4())
    uid_c = str(uuid.uuid4())
    base = {"code": "EMP-0100", "department_uid": DEPT_OPS, "hire_date": "2026-01-05",
            "status": "active", "nationality": "egyptian"}
    ctx.client("B").local_change(op="create", entity="employees", uid=uid_b,
                                 value=dict(base, full_name="Assistant Hire"))
    ctx.client("C").local_change(op="create", entity="employees", uid=uid_c,
                                 value=dict(base, full_name="Finance Hire"))
    b = sync(ctx, "B")
    c = sync(ctx, "C")
    checks.eq("first create applied", b["applied"], [1])
    checks.eq("second create corrected", [(x["old"], x["new"]) for x in c["code_corrections"]],
              [("EMP-0100", "EMP-0101")])
    converge(ctx)
    checks.eq("main kept the first code", ctx.value("A", ROLE_ADMIN, "employees", uid_b, "code"),
              "EMP-0100")
    checks.eq("main gave the second device the next code",
              ctx.value("A", ROLE_ADMIN, "employees", uid_c, "code"), "EMP-0101")
    checks.eq("the device corrected its own row",
              ctx.value("C", ROLE_FINANCE, "employees", uid_c, "code"), "EMP-0101")
    with ctx.engine("A").connect() as connection:
        checks.eq("no duplicate codes", connection.execute(
            text("SELECT count(*) FROM (SELECT code FROM employees GROUP BY code HAVING count(*) > 1) d")
        ).scalar(), 0)
    incomplete = [row["uid"] for row in ctx.state("A")["incomplete"]]
    checks.check("assistant-created employee waits for the administrator (rule 8)",
                 uid_b in incomplete, incomplete)
    assert_converged(ctx, checks, label="duplicate code")


def s_role_visibility(ctx: Context, checks: Checks) -> None:
    """Hidden fields never reach a device, and a forged hidden edit is rejected (rule 5)."""
    converge(ctx)
    assistant = invariants.projection(ctx.engine("B"), ROLE_ASSISTANT)
    finance = invariants.projection(ctx.engine("C"), ROLE_FINANCE)
    hidden_for_assistant = {"basic_salary", "national_id", "phone", "address", "position",
                            "birth_date", "passport_number", "fingerprint_id"}
    checks.check("the assistant device holds no private employee fields",
                 not any(hidden_for_assistant & set(values) for values in assistant.values()),
                 [sorted(hidden_for_assistant & set(values)) for values in assistant.values()][:3])
    main_view = invariants.projection(ctx.engine("A"), ROLE_ASSISTANT)
    checks.check("the main device derives the branch name for the assistant",
                 main_view[("employees", E1)].get("branch_name") == "Main Branch",
                 main_view[("employees", E1)].get("branch_name"))
    checks.check("the finance device holds salary",
                 finance[("employees", E1)].get("basic_salary") == "5000.00")
    checks.check("the finance device holds no assistant-only column",
                 not any("branch_name" in values for values in finance.values()))

    ctx.client("A").pair("device-x", ROLE_ASSISTANT)
    forged = raw_session(ctx, device_id="device-x", role=ROLE_ASSISTANT, changes=forged_changes(
        "device-x",
        [("set", "employees", E1, "basic_salary", "5000.00", "99999.00"),
         ("set", "employees", E1, "full_name", "Seed One", "Legit Edit")]))
    checks.eq("the forged salary edit is rejected", forged["rejected"],
              [{"seq": 1, "reason": "field_not_visible"}])
    checks.eq("the legitimate change in the same session still applies", forged["applied"], [2])
    checks.eq("salary did not move", ctx.value("A", ROLE_ADMIN, "employees", E1, "basic_salary"),
              "5000.00")
    checks.eq("the administrator is alerted", len(invariants.alerts(ctx.engine("A"),
                                                                    "hidden_field_edit")), 1)
    forged_create = raw_session(ctx, device_id="device-x", role=ROLE_ASSISTANT, changes=forged_changes(
        "device-x", start_seq=3, prev=_head_of(ctx, "device-x"), entries=[("create", "employees", str(uuid.uuid4()), "", None,
                      {"code": "EMP-0200", "full_name": "Forged", "department_uid": DEPT_OPS,
                       "hire_date": "2026-02-01", "status": "active", "basic_salary": "1.00"})]))
    checks.eq("a create carrying a hidden field is rejected",
              [r["reason"] for r in forged_create["rejected"]], ["field_not_visible"])
    with ctx.engine("A").connect() as connection:
        checks.eq("nothing was created", connection.execute(
            text("SELECT count(*) FROM employees WHERE code = 'EMP-0200'")).scalar(), 0)


def s_restore_refusal(ctx: Context, checks: Checks) -> None:
    """An older backup restored on main refuses sync until the administrator decides (rule 9)."""
    converge(ctx)
    dump = ctx.work_dir / "main-backup.dump"
    ctx.provisioner.server("A").dump(provision.DATABASES["A"], dump)
    checks.check("the backup file exists", dump.exists() and dump.stat().st_size > 0)

    ctx.client("A").local_change(op="set", entity="employees", uid=E1, field="phone",
                                 value="01099999999")
    ctx.client("B").local_change(op="set", entity="employees", uid=E1, field="full_name",
                                 value="After The Backup")

    ctx.supervisor.stop("A")
    ctx.provisioner.server("A").restore(provision.DATABASES["A"], dump)
    epoch = bump_restore_epoch(ctx.engine("A"))
    ctx.supervisor.start("A")
    checks.eq("the restore brought back the older data",
              ctx.value("A", ROLE_ADMIN, "employees", E1, "phone"), "01000000001")
    before = invariants.snapshot(ctx.engine("A"), ROLE_ADMIN)
    summary = sync(ctx, "B")
    checks.eq("sync is refused after the restore", summary["status"], "refused_restore")
    checks.check("the refusal says what happened",
                 "restore" in (summary["message"] or "").lower(), summary["message"])
    invariants.assert_unchanged(before, ctx.engine("A"), ROLE_ADMIN, label="restore refusal")
    checks.eq("the device's edit was not applied", ctx.value(
        "A", ROLE_ADMIN, "employees", E1, "full_name"), "Seed One")
    with ctx.engine("B").connect() as connection:
        checks.eq("the device keeps its edit queued", connection.execute(
            text("SELECT state FROM harness_sync_outbox WHERE seq = 1")).scalar(), "queued")

    ctx.client("A").rebaseline("device-b")
    ctx.client("B").device_rebaseline(epoch)
    sync(ctx, "B")
    converge(ctx)
    checks.eq("after the decision the device matches the restored main device",
              invariants.differences(ctx.engine("A"), ctx.engine("B"), ROLE_ASSISTANT), [])
    with ctx.engine("B").connect() as connection:
        checks.eq("the device's unsent edit is kept as a conflict, not dropped",
                  connection.execute(text("SELECT count(*) FROM harness_sync_conflict "
                                          "WHERE kind = 'restore_rebaseline'")).scalar(), 1)
    checks.eq("the restored main device was not overwritten",
              ctx.value("A", ROLE_ADMIN, "employees", E1, "full_name"), "Seed One")


def s_replay(ctx: Context, checks: Checks) -> None:
    """Sending the same session twice changes nothing."""
    converge(ctx)
    ctx.client("B").local_change(op="set", entity="employees", uid=E1, field="full_name",
                                 value="Replay Edit")
    summary = sync(ctx, "B")
    request = summary["request"]
    before = snapshot_all(ctx)
    again = post_json(ctx.main_url + "/sync/session", request)
    checks.eq("the replay is recognised", again["replayed"], True)
    checks.eq("the replay changes nothing", snapshot_all(ctx), before)

    second = dict(request, session_id=str(uuid.uuid4()))
    duplicate = post_json(ctx.main_url + "/sync/session", second)
    checks.eq("a new session with the same change is deduplicated by seq",
              duplicate["duplicate"], [1])
    checks.eq("and applies nothing", duplicate["applied"], [])
    checks.eq("state still unchanged", snapshot_all(ctx), before)


def s_clock_skew(ctx: Context, checks: Checks) -> None:
    """The main device stamps when it received a change, whatever the device's clock says (rule 10)."""
    ctx.supervisor.stop("B")
    ctx.supervisor.spec("B").clock_offset_seconds = 7200
    ctx.supervisor.start("B")
    converge(ctx)
    ctx.client("B").local_change(op="set", entity="employees", uid=E1, field="full_name",
                                 value="Skewed Clock Edit")
    summary = sync(ctx, "B")
    checks.eq("the skewed device's change applied", summary["applied"], [1])
    with ctx.engine("A").connect() as connection:
        row = connection.execute(text(
            "SELECT received_at, local_time, extract(epoch from (now() - received_at)) AS age, "
            "extract(epoch from (local_time - received_at)) AS skew "
            "FROM harness_sync_change WHERE device_id = 'device-b' ORDER BY id DESC LIMIT 1"
        )).mappings().first()
    checks.check("the main device's receive time is its own clock", abs(row["age"]) < 60, row["age"])
    checks.check("the device's clock really was two hours ahead", abs(row["skew"] - 7200) < 300,
                 row["skew"])
    checks.eq("the value itself applied normally", ctx.value(
        "A", ROLE_ADMIN, "employees", E1, "full_name"), "Skewed Clock Edit")


def s_randomized(ctx: Context, checks: Checks) -> None:
    """Seeded random operations, disconnects and decisions; invariants at the end."""
    rng = random.Random(ctx.seed)
    converge(ctx)
    alive = {E1, E2, E3}
    counter = 0
    for step in range(int(ctx.notes.get("random_steps", 18)) if hasattr(ctx, "notes") else 18):
        counter += 1
        actor = rng.choice(["A", "B", "C"])
        role = ROLES[actor]
        choice = rng.random()
        if choice < 0.55:
            uid = rng.choice(sorted(alive))
            fields = sorted(sync_visibility.visible_columns("employees", role) & {
                "full_name", "status", "nationality", "address", "phone", "basic_salary",
                "position", "hire_date", "department_id"})
            if not fields:
                continue
            field = rng.choice(fields)
            if field == "department_id":
                field = "department_uid"
                value = rng.choice([DEPT_OPS, DEPT_FIN])
            elif field == "basic_salary":
                value = f"{rng.randrange(3000, 9000)}.00"
            elif field == "hire_date":
                value = f"20{rng.randrange(20, 26)}-0{rng.randrange(1, 10)}-01"
            elif field == "status":
                value = rng.choice(["active", "on_leave", "suspended"])
            elif field == "nationality":
                value = "egyptian"
            elif field == "phone":
                value = f"010{rng.randrange(10000000, 99999999)}"
            else:
                value = f"{field} {counter}"
            try:
                ctx.client(actor).local_change(op="set", entity="employees", uid=uid,
                                               field=field, value=value)
            except HttpError as exc:
                if "does not exist" not in exc.body:   # a record not synced here yet
                    raise
        elif choice < 0.65 and E3 in alive:
            try:
                ctx.client(actor).local_change(op="delete", entity="employees", uid=E3)
                alive.discard(E3)
            except Exception:
                alive.add(E3)
        elif choice < 0.75:
            uid = str(uuid.uuid4())
            payload = {"code": f"EMP-{rng.randrange(100, 200):04d}", "department_uid": DEPT_OPS,
                       "hire_date": "2026-03-01", "status": "active", "nationality": "egyptian",
                       "full_name": f"Random Hire {counter}"}
            if role in (ROLE_ADMIN, ROLE_FINANCE):
                payload.update({"position": "Clerk", "basic_salary": "3000.00",
                                "daily_hours": "8"})
            ctx.client(actor).local_change(op="create", entity="employees", uid=uid, value=payload)
            alive.add(uid)
        elif choice < 0.85:
            pending = ctx.pending_conflicts()
            if pending:
                conflict = rng.choice(pending)
                ctx.client("A").resolve_conflict(conflict["id"],
                                                 rng.choice(["accept", "reject"]))
        elif choice < 0.92:
            target = rng.choice(["B", "C"])
            ctx.supervisor.restart(target)
        else:
            target = rng.choice(["B", "C"])
            sync(ctx, target)
    converge(ctx, rounds=3)
    if ctx.notes.get("trace"):
        print("TRACE conflicts", [(c["id"], c["kind"], c["field"], c["device_id"], c["status"]) for c in ctx.conflicts()])
        with ctx.engine("A").connect() as connection:
            for r in connection.execute(text("SELECT counter, device_id, op, field, new_value, status FROM harness_sync_change WHERE field='department_uid' ORDER BY counter")).all(): print("TRACE log", tuple(r))
        for n in ("B","C"):
            print("TRACE", n, ctx.state(n)["outbox"])
    for conflict in ctx.pending_conflicts():
        ctx.client("A").resolve_conflict(conflict["id"], rng.choice(["accept", "reject"]))
    converge(ctx, rounds=3)

    assert_converged(ctx, checks, label=f"random seed {ctx.seed}")
    checks.eq("no conflict is left pending", ctx.pending_conflicts(), [])
    for name in ("B", "C"):
        with ctx.engine(name).connect() as connection:
            checks.eq(f"device {name} has nothing left queued", connection.execute(
                text("SELECT count(*) FROM harness_sync_outbox WHERE state = 'queued'")).scalar(), 0)
    _check_no_lost_change(ctx, checks)
    _check_no_silent_overwrite(ctx, checks)


def _check_no_lost_change(ctx: Context, checks: Checks) -> None:
    """The last accepted change to a field is the value every database holds."""
    with ctx.engine("A").connect() as connection:
        last = connection.execute(text(
            "SELECT entity, uid, field, new_value FROM ("
            "  SELECT entity, uid, field, new_value,"
            "         row_number() OVER (PARTITION BY entity, uid, field ORDER BY counter DESC) AS rn"
            "  FROM harness_sync_change WHERE status = 'applied' AND op = 'set') ranked "
            "WHERE rn = 1")).mappings().all()
        problems = []
        for row in last:
            if row["entity"] != "employees":
                continue
            record = store.row_for_uid(connection, "employees", row["uid"])
            if record is None:
                continue
            current = store.row_payload(connection, "employees", record, ROLE_ADMIN).get(row["field"])
            if json.loads(row["new_value"]) != current:
                problems.append(f"{row['uid']}.{row['field']}: accepted {row['new_value']} "
                                f"but holds {json.dumps(current)}")
        checks.check("every accepted change is the value the main device holds",
                     not problems, problems[:8])


def _check_no_silent_overwrite(ctx: Context, checks: Checks) -> None:
    """No record or value exists on main without a recorded change behind it."""
    seeded = {E1, E2, E3}
    with ctx.engine("A").connect() as connection:
        created = {row[0] for row in connection.execute(text(
            "SELECT uid FROM harness_sync_change WHERE op = 'create' AND status = 'applied'"))}
        rows = connection.execute(text("SELECT sync_uid, code FROM employees")).all()
        unexplained = [f"{uid} ({code})" for uid, code in rows
                       if uid not in seeded and uid not in created]
        checks.check("every record on the main device came from the seed or a recorded create",
                     not unexplained, unexplained[:8])
        deleted_twice = connection.execute(text(
            "SELECT count(*) FROM (SELECT uid FROM harness_sync_change WHERE op = 'delete' "
            "AND status = 'applied' GROUP BY uid HAVING count(*) > 1) d")).scalar()
        checks.eq("no record was deleted twice", deleted_twice, 0)


SCENARIOS = (
    ("two_way_edits", s_two_way_edits),
    ("different_fields_same_record", s_different_fields_same_record),
    ("same_field_conflict_accept", s_same_field_conflict_accept),
    ("same_field_conflict_reject", s_same_field_conflict_reject),
    ("delete_vs_edit", s_delete_vs_edit),
    ("edit_vs_edit_delete_accepted", s_edit_vs_edit_delete_accepted),
    ("offline_reconnect", s_offline_reconnect),
    ("interrupted_before_send", s_interrupted_before_send),
    ("interrupted_during_receive", s_interrupted_during_receive),
    ("interrupted_before_confirm", s_interrupted_before_confirm),
    ("interrupted_device_mid_apply", s_interrupted_device_mid_apply),
    ("interrupted_main_mid_apply", s_interrupted_main_mid_apply),
    ("interrupted_main_before_commit", s_interrupted_main_before_commit),
    ("main_rollback_is_atomic", s_main_rollback_is_atomic),
    ("duplicate_code", s_duplicate_code),
    ("role_visibility", s_role_visibility),
    ("restore_refusal", s_restore_refusal),
    ("replay", s_replay),
    ("clock_skew", s_clock_skew),
    ("randomized", s_randomized),
)
