"""Adapter for the real applier, once build order items 3 to 5 land.

The harness talks to whichever engine it is told to use through engines.Engine.
Today the module ``app.sync_session`` does not exist, so ``--engine auto`` falls
back to the reference engine and the report says so. When the writer's apply
entry points land, they only need to expose the functions listed below under that
module name, and ``--engine app`` runs the same scenarios against the product.

Nothing here changes app/; it is an import probe plus argument adaptation. The
harness supplies the transaction: every call happens inside one
``engine.begin()`` block on that instance's database.
"""

from __future__ import annotations

from sqlalchemy.orm import Session


class HarnessUnavailable(RuntimeError):
    """The real applier is not in this checkout (or does not expose the interface)."""


ENTRY_POINTS = (
    # main device
    "apply_session",          # (db, *, device_id, role, epoch, base_counter, changes) -> response dict
    "record_confirmation",    # (db, *, device_id, session_id, applied_counter) -> None
    "resolve_conflict",       # (db, *, conflict_id, decision, actor) -> None
    "local_change",           # (db, *, device_id, op, entity, uid, field, value) -> None
    "complete_employee",      # (db, *, uid, values) -> None
    "rebaseline_device",      # (db, *, device_id) -> dict
    # device
    "device_queue_change",    # (db, *, op, entity, uid, field, value, local_time) -> dict
    "device_build_request",   # (db, *, session_id) -> session dict
    "device_apply_response",  # (db, response, *, role) -> None
    "device_apply_pull",      # (db, items, *, role) -> int (new applied counter)
    "device_rebaseline",      # (db, *, main_epoch) -> dict
)


def load():
    try:
        from app import sync_session as module  # type: ignore
    except ImportError as exc:
        raise HarnessUnavailable(
            "app.sync_session is not in this checkout, so the real applier cannot be driven "
            "yet (docs/SYNC_DESIGN.md build order items 3 to 5). Run with --engine reference."
        ) from exc
    missing = [name for name in ENTRY_POINTS if not callable(getattr(module, name, None))]
    if missing:
        raise HarnessUnavailable(f"app.sync_session is missing {sorted(missing)}")
    return module


class AppEngine:
    """Calls the product's entry points; the harness supplies the transaction."""

    name = "app"

    def __init__(self):
        self.module = load()

    def _db(self, connection) -> Session:
        return Session(bind=connection)

    def local_change(self, connection, *, op, entity, uid, field="", value=None,
                     device_id="main"):
        self.module.local_change(self._db(connection), device_id=device_id, op=op, entity=entity,
                                 uid=uid, field=field, value=value)

    def apply_session(self, connection, request, *, session_id, fault=None):
        return self.module.apply_session(
            self._db(connection), device_id=request["device_id"], role=request["role"],
            epoch=request.get("epoch"), base_counter=request["base_counter"],
            changes=request["changes"],
        )

    def confirm(self, connection, *, session_id, device_id, applied_counter):
        self.module.record_confirmation(self._db(connection), device_id=device_id,
                                        session_id=session_id, applied_counter=applied_counter)

    def resolve_conflict(self, connection, *, conflict_id, decision):
        self.module.resolve_conflict(self._db(connection), conflict_id=conflict_id,
                                     decision=decision, actor="admin")

    def complete_employee(self, connection, *, uid, values):
        self.module.complete_employee(self._db(connection), uid=uid, values=values)

    def rebaseline_device(self, connection, *, device_id):
        return self.module.rebaseline_device(self._db(connection), device_id=device_id)

    def device_queue_change(self, connection, *, op, entity, uid, field="", value=None,
                            local_time=None):
        return self.module.device_queue_change(self._db(connection), op=op, entity=entity, uid=uid,
                                               field=field, value=value, local_time=local_time)

    def device_build_request(self, connection, *, session_id):
        return self.module.device_build_request(self._db(connection), session_id=session_id)

    def device_apply_response(self, connection, response, *, role):
        self.module.device_apply_response(self._db(connection), response, role=role)

    def device_apply_pull(self, connection, items, *, role, fault=None):
        return self.module.device_apply_pull(self._db(connection), items, role=role)

    def device_rebaseline(self, connection, *, main_epoch):
        return self.module.device_rebaseline(self._db(connection), main_epoch=main_epoch)
