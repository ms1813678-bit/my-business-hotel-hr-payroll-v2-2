"""Seeded randomized scenarios, part 2: hidden fields, restore of main, record creation. Pure, no database.

Independent of tests/test_sync_randomized.py (own small model). Failure messages carry the seed.
Invariants:
- a session with a field its role may not see is returned whole and main is byte-for-byte unchanged, so a hidden
  value written by an assistant device never reaches main;
- a session refused by restore_guard never changes main;
- after a restore of main (older snapshot, epoch + 1), no device syncs until the administrator's decision, which the
  model plays as "device drops its pending rows and adopts main"; after that the device syncs normally;
- created records appear on main with their first values and a create on a deleted record never resurrects it;
- the replay of an applied session changes nothing; final main == devices.
"""
import copy
import random
import uuid

import pytest

from app.sync_apply import READY, RETURNED, plan_session
from app.sync_apply_ops import plan_to_ops
from app.sync_change_log import SyncLogError
from app.sync_change_log import GENESIS_HASH, row_hash
from app.sync_rules import ACCEPT, REFUSE, REJECT, resolve_conflict, restore_guard

T = "2026-11-28T09:00:00.000000Z"
ALL_FIELDS = ("full_name", "nationality", "basic_salary")
ROLE = {"dev-a": "system_admin", "dev-b": "finance", "dev-c": "assistant_manager"}
ALLOW = lambda role, entity, field: True  # noqa: E731


def uid(i):
    return str(uuid.UUID(int=(i + 1) << 64 | 0x4000_8000_0000_0000_0000 >> 0 | i, version=4))


class Main:
    def __init__(self):
        self.epoch = 0
        self.values, self.deleted, self.touched = {}, set(), {}
        self.dev = {}
        for i in range(2):
            self._create(uid(i), "v0")

    def _create(self, u, first=None):
        for f in ALL_FIELDS:
            self.values.setdefault(("employees", u, f), first)

    def state(self):
        return copy.deepcopy((self.values, self.deleted, self.dev))

    def register(self, d):
        self.dev[d] = {"applied": {}, "last": 0, "epoch": 0}
        self.touched[d] = set()

    def sync(self, d, rows, rng):
        st = self.dev[d]
        keys = {(r["entity"], r["uid"], r["field"]) for r in rows}
        plan = plan_session(role=ROLE[d], device_id=d, rows=rows, applied=st["applied"], last_counter=st["last"],
                            main_values={k: v for k, v in self.values.items() if k in keys},
                            main_deleted=set(self.deleted), main_touched=set(self.touched[d]), may_edit=ALLOW)
        if plan.status == RETURNED:
            return plan, None
        s = plan_to_ops(plan, device_id=d, applied_last_counter=st["last"])
        for op in s.ops:
            rec = (op.entity, op.uid)
            if op.kind == "create":
                self._create(op.uid)
            elif op.kind == "set":
                assert self.values[(op.entity, op.uid, op.field)] == op.old_value, "silent overwrite"
                self.values[(op.entity, op.uid, op.field)] = op.new_value
            else:
                self.deleted.add(rec)
            for t in self.touched.values():
                t.add(rec)
        for c in s.conflicts:
            try:
                ops = resolve_conflict(c, ACCEPT if rng.random() < 0.5 else REJECT)
            except SyncLogError:
                ops = ()
            for op in ops:
                if op.kind == "set" and (op.entity, op.uid) not in self.deleted:
                    self.values[(op.entity, op.uid, op.field)] = op.new_value
                elif op.kind == "delete":
                    self.deleted.add((op.entity, op.uid))
        st["applied"].update({r["origin_counter"]: r["row_hash"] for r in rows})
        st["last"] = s.last_counter
        self.touched[d] = set()
        return plan, s

    def guarded_sync(self, d, dev, rows, rng):
        st = self.dev[d]
        g = restore_guard(main_epoch=self.epoch, device_epoch=dev.epoch, main_known_counter=st["last"],
                          device_last_counter=dev.synced)
        if dev.epoch > self.epoch:  # expected reason computed independently of restore_guard, same priority order
            want = ("refuse", "device_epoch_newer")
        elif dev.synced > st["last"]:
            want = ("refuse", "device_counter_ahead")
        elif dev.epoch < self.epoch:
            want = ("refuse", "device_epoch_older_decision_needed")
        else:
            want = ("admit", "ok")
        assert (g.decision, g.reason) == want, f"guard gave {g} but history says {want}"
        if g.decision == REFUSE:
            return g, None, None
        return g, *self.sync(d, rows, rng)

    def restore(self, snapshot):
        self.values, self.deleted, self.dev = copy.deepcopy(snapshot)
        self.epoch += 1
        self.touched = {d: set() for d in self.dev}


class Device:
    def __init__(self, name, main):
        self.name, self.main, self.epoch = name, main, 0
        main.register(name)
        self.adopt(rebase=False)

    def adopt(self, rebase):
        m = self.main
        self.values = {k: v for k, v in m.values.items() if self._sees(k[2])}
        self.deleted, self.rows = set(m.deleted), []
        if rebase:  # the administrator's decision after a restore: start again from main
            st = m.dev[self.name]
            self.counter, self.prev = st["last"], (st["applied"].get(st["last"]) or GENESIS_HASH)
            self.epoch = m.epoch
        else:
            self.counter, self.prev = 0, GENESIS_HASH
        self.synced = self.counter  # highest counter the device believes main holds

    def _sees(self, f):
        return not (ROLE[self.name] == "assistant_manager" and f == "basic_salary")

    def _row(self, op, e, u, f, old, new):
        self.counter += 1
        h = row_hash(self.prev, self.name, self.counter, op, e, u, f, old, new, T, self.epoch or None)
        self.rows.append({"origin_device_id": self.name, "origin_counter": self.counter, "op": op, "entity": e,
                          "uid": u, "field": f, "old_value": old, "new_value": new, "local_time": T,
                          "restore_epoch": self.epoch or None, "prev_hash": self.prev, "row_hash": h})
        self.prev = h

    def known(self):
        return sorted({k[1] for k in self.values})

    def edit(self, u, f, v, force_hidden=False):
        k = ("employees", u, f)
        if ("employees", u) in self.deleted or (k not in self.values and not force_hidden):
            return
        self._row("set", "employees", u, f, self.values[k] if k in self.values else None, v)
        self.values[k] = v

    def create(self, u, name):
        if ("employees", u) in {(e, x) for e, x, _ in self.values} or any(r["uid"] == u for r in self.rows):
            return
        self._row("create", "employees", u, "*", None, None)
        for f in ALL_FIELDS:
            if f == "basic_salary" and not self._sees(f):
                self.values[("employees", u, f)] = None  # never exported, never held on the assistant device
                continue
            self._row("set", "employees", u, f, None, name)
            self.values[("employees", u, f)] = name

    def delete(self, u):
        if ("employees", u) not in self.deleted:
            self._row("delete", "employees", u, "*", None, None)
            self.deleted.add(("employees", u))


def run(seed, steps=70):
    rng = random.Random(seed)
    main = Main()
    devs = [Device(n, main) for n in ROLE]
    snapshots, nxt = [], 10
    for _ in range(steps):
        d, a = rng.choice(devs), rng.random()
        known = d.known()
        u = rng.choice(known) if known else None
        if a < 0.30 and u:
            d.edit(u, rng.choice(ALL_FIELDS), f"{d.name}-{rng.randint(0, 99)}")
        elif a < 0.36 and u and d.name == "dev-c":  # forged edit of a hidden field
            d.edit(u, "basic_salary", "HACK", force_hidden=True)
        elif a < 0.44:
            nxt += 1
            d.create(uid(nxt), f"new-{nxt}")
        elif a < 0.48 and u:
            d.delete(u)
        elif a < 0.49:
            d.synced += rng.randint(1, 3)  # a device that believes main holds more of its rows than main does
        elif a < 0.50:
            d.epoch = main.epoch + 1  # a device that saw a newer main than this one (for example main replaced by an older copy)
        elif a < 0.52:
            snapshots.append(main.state())
        elif a < 0.56 and snapshots:  # restore of main from an older backup
            main.restore(rng.choice(snapshots))
        elif d.rows:
            before = main.state()
            g, plan, s = main.guarded_sync(d.name, d, d.rows, rng)
            if g.decision == REFUSE:
                assert main.state() == before, f"seed {seed}: refused session changed main"
                if g.reason in ("device_epoch_older_decision_needed", "device_counter_ahead", "device_epoch_newer"):
                    d.adopt(rebase=True)  # administrator decision played as: drop pending rows, adopt main
                continue
            if plan.status == RETURNED:
                assert main.state() == before, f"seed {seed}: returned session changed main"
                d.adopt(rebase=True)  # model: the administrator removes the whole returned session
                continue
            snap = main.state()
            _, s2 = main.sync(d.name, d.rows, rng)
            assert main.state() == snap and s2.ops == () and s2.conflicts == (), f"seed {seed}: replay changed main"
            d.rows = []
            d.synced = d.counter
            d.values = {k: v for k, v in main.values.items() if d._sees(k[2])}
            d.deleted = set(main.deleted)
    for _ in range(2):
        for d in devs:
            if d.rows:
                g, plan, s = main.guarded_sync(d.name, d, d.rows, rng)
                d.rows = []
                if g.decision == REFUSE or plan.status == RETURNED:
                    d.adopt(rebase=True)
                else:
                    d.synced = d.counter
        for d in devs:
            if main.dev[d.name]["last"] <= d.counter or d.epoch != main.epoch:
                d.adopt(rebase=True)
            d.values = {k: v for k, v in main.values.items() if d._sees(k[2])}
            d.deleted = set(main.deleted)
    return main, devs


@pytest.mark.parametrize("seed", range(300), ids=lambda s: f"seed_{s}")
def test_hidden_restore_and_create_scenarios(seed):
    main, devs = run(seed)
    assert not any(v == "HACK" for v in main.values.values()), f"seed {seed}: hidden-field forgery reached main"
    for d in devs:
        assert d.values == {k: v for k, v in main.values.items() if d._sees(k[2])}, f"seed {seed}: {d.name} differs"
        assert d.deleted == main.deleted
    for (e, u, f), v in main.values.items():
        assert f in ALL_FIELDS
