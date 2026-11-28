"""Seeded randomized scenarios over the pure sync pieces (plan_session, plan_to_ops, resolve_conflict).

No database. A tiny in-memory main and two devices. Every failure message carries the seed, so
`pytest -k "seed_17"` repeats it. Invariants checked:
- an applied set always had old_value equal to main's current value (no silent overwrite);
- replaying a session already applied changes nothing;
- a cut-short session is a valid prefix and a replay of an applied one changes nothing (in this model the main state changes
  only when a whole session is applied; real crash safety needs the database executor and its own tests);
- an accepted conflict leaves main with the device's new value (or the record deleted); a rejected one leaves main unchanged;
- after a final clean sync of both devices, main and both devices are equal, and every field's value on main is
  the last value written to it by an applied op, an accepted conflict or a main edit (no lost accepted change).
"""
import random
import uuid

import pytest

from app.sync_apply import READY, plan_session
from app.sync_apply_ops import plan_to_ops
from app.sync_change_log import GENESIS_HASH, SyncLogError, row_hash
from app.sync_rules import ACCEPT, REJECT, resolve_conflict

EMPS = [str(uuid.UUID(int=(i + 1) << 64 | 0x4000_8000_0000_0000_0000 >> 0 | i, version=4)) for i in range(3)]
FIELDS = ("full_name", "nationality")
T = "2026-11-28T09:00:00.000000Z"
ALLOW = lambda role, entity, field: True  # noqa: E731


class Main:
    def __init__(self):
        self.values = {("employees", u, f): "v0" for u in EMPS for f in FIELDS}
        self.deleted = set()
        self.last_written = dict(self.values)
        self.dev = {}  # device -> {"applied": {c: hash}, "last": int}
        self.touched = {}  # device -> records main changed since that device's last sync

    def register(self, d):
        self.dev[d] = {"applied": {}, "last": 0}
        self.touched[d] = set()

    def main_edit(self, u, f, v):
        if ("employees", u) in self.deleted:
            return
        self.values[("employees", u, f)] = self.last_written[("employees", u, f)] = v
        for d in self.touched:
            self.touched[d].add(("employees", u))

    def _write(self, op, who):
        rec = (op.entity, op.uid)
        if op.kind == "set":
            key = (op.entity, op.uid, op.field)
            assert self.values[key] == op.old_value, who + ": silent overwrite"
            self.values[key] = self.last_written[key] = op.new_value
        elif op.kind == "delete":
            self.deleted.add(rec)
        for d in self.touched:
            self.touched[d].add(rec)

    def sync(self, device, rows, rng, plan_only=False):
        st = self.dev[device]
        keys = {(r["entity"], r["uid"], r["field"]) for r in rows}
        mv = {k: v for k, v in self.values.items() if k in keys}
        plan = plan_session(role="system_admin", device_id=device, rows=rows, applied=st["applied"],
                            last_counter=st["last"], main_values=mv, main_deleted=set(self.deleted),
                            main_touched=set(self.touched[device]), may_edit=ALLOW)
        assert plan.status == READY
        s = plan_to_ops(plan, device_id=device, applied_last_counter=st["last"])
        for op in s.ops:
            self._write(op, device)
        for c in s.conflicts:
            decision = ACCEPT if rng.random() < 0.5 else REJECT
            before = (dict(self.values), set(self.deleted))
            try:
                ops = resolve_conflict(c, decision)
            except SyncLogError:
                ops = ()  # accept refused by rule (edit or create on a deleted record): stays rejected
            if decision == ACCEPT and c["reason"] in ("same_field_changed", "delete_against_edit"):
                assert len(ops) == 1, "accept of an acceptable conflict gave no operation"
            if decision == REJECT:
                assert ops == () and (self.values, self.deleted) == before, "reject changed main"
            for op in ops:
                key = (op.entity, op.uid, op.field)
                if op.kind == "set" and (op.entity, op.uid) in self.deleted:
                    continue
                if op.kind == "set":
                    self.values[key] = self.last_written[key] = op.new_value
                    assert self.values[key] == c["new_value"], "accepted set is not the device value"
                else:
                    self._write(op, device)
                    assert (op.entity, op.uid) in self.deleted, "accepted delete did not delete"
        by_counter = {r["origin_counter"]: r["row_hash"] for r in rows}
        st["applied"].update(by_counter)
        st["last"] = s.last_counter
        self.touched[device] = set()
        return None, s


class Device:
    def __init__(self, name, main):
        self.name, self.main = name, main
        main.register(name)
        self.values, self.deleted = dict(main.values), set(main.deleted)
        self.rows, self.prev, self.counter = [], GENESIS_HASH, 0  # rows not yet applied on main

    def _row(self, op, entity, uid, field, old, new):
        self.counter += 1
        h = row_hash(self.prev, self.name, self.counter, op, entity, uid, field, old, new, T, None)
        r = {"origin_device_id": self.name, "origin_counter": self.counter, "op": op, "entity": entity, "uid": uid,
             "field": field, "old_value": old, "new_value": new, "local_time": T, "restore_epoch": None,
             "prev_hash": self.prev, "row_hash": h}
        self.prev = h
        self.rows.append(r)

    def edit(self, u, f, v):
        if ("employees", u) in self.deleted:
            return
        k = ("employees", u, f)
        self._row("set", "employees", u, f, self.values[k], v)
        self.values[k] = v

    def delete(self, u):
        if ("employees", u) in self.deleted:
            return
        self._row("delete", "employees", u, "*", None, None)
        self.deleted.add(("employees", u))

    def pull(self):
        self.values, self.deleted = dict(self.main.values), set(self.main.deleted)


def run(seed, steps=60):
    rng = random.Random(seed)
    main = Main()
    devs = [Device("dev-a", main), Device("dev-b", main)]
    n = 0
    for _ in range(steps):
        act = rng.random()
        d = rng.choice(devs)
        u, f = rng.choice(EMPS), rng.choice(FIELDS)
        if act < 0.35:
            n += 1
            d.edit(u, f, f"{d.name}-{n}")
        elif act < 0.5:
            n += 1
            main.main_edit(u, f, f"main-{n}")
        elif act < 0.55:
            d.delete(u)
        else:
            if not d.rows:
                continue
            cut = rng.randint(0, len(d.rows))  # a session cut short: a valid prefix, or nothing
            sent = d.rows[:cut]
            if rng.random() < 0.2:  # lost reply: the same session arrives twice
                main.sync(d.name, sent, rng)
            if sent:
                main.sync(d.name, sent, rng)
                if rng.random() < 0.3:  # replay of an already applied session
                    snap = (dict(main.values), set(main.deleted))
                    _, s2 = main.sync(d.name, sent, rng)
                    assert (main.values, main.deleted) == snap, f"seed {seed}: replay changed main"
                    assert s2.ops == () and s2.conflicts == (), f"seed {seed}: replay produced work"
            d.rows = d.rows[len(sent):]
            if rng.random() < 0.5:
                d.pull()
    for _ in range(2):  # final clean rounds: every device pushes, then every device pulls
        for d in devs:
            if d.rows:
                main.sync(d.name, d.rows, rng)
                d.rows = []
        for d in devs:
            d.pull()
    return main, devs


@pytest.mark.parametrize("seed", range(300), ids=lambda s: f"seed_{s}")
def test_randomized_scenarios_converge_without_loss(seed):
    main, devs = run(seed)
    for d in devs:
        assert d.values == main.values and d.deleted == main.deleted, f"seed {seed}: {d.name} differs from main"
    for k, v in main.values.items():
        assert v == main.last_written[k], f"seed {seed}: {k} is not the last written value"
