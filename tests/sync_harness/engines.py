"""Engine selection: the reference implementation or the product's applier.

Both sides of a session go through this interface, so a scenario never knows
which engine it is driving. ``--engine auto`` uses app.sync_session when this
checkout has it and says which engine ran in the report.
"""

from __future__ import annotations

from . import app_engine, device_engine, reference_engine


class ReferenceEngine:
    """The test double (reference_engine.py + device_engine.py)."""

    name = "reference"

    def local_change(self, connection, *, op, entity, uid, field="", value=None,
                     device_id="main"):
        return reference_engine.main_local_change(connection, op=op, entity=entity, uid=uid,
                                                  field=field, value=value, actor=device_id)

    def apply_session(self, connection, request, *, session_id, fault=None):
        return reference_engine.apply_session(connection, request, session_id=session_id, fault=fault)

    def confirm(self, connection, *, session_id, device_id, applied_counter):
        return reference_engine.confirm_session(connection, session_id=session_id,
                                                device_id=device_id,
                                                applied_counter=applied_counter)

    def resolve_conflict(self, connection, *, conflict_id, decision):
        return reference_engine.resolve_conflict(connection, conflict_id=conflict_id,
                                                 decision=decision)

    def complete_employee(self, connection, *, uid, values):
        return reference_engine.complete_employee(connection, uid=uid, values=values)

    def rebaseline_device(self, connection, *, device_id):
        return reference_engine.rebaseline_device(connection, device_id=device_id)

    def device_queue_change(self, connection, *, op, entity, uid, field="", value=None,
                            local_time=None):
        return device_engine.queue_local_change(connection, op=op, entity=entity, uid=uid,
                                                field=field, value=value, local_time=local_time)

    def device_build_request(self, connection, *, session_id):
        return device_engine.build_request(connection, session_id=session_id)

    def device_apply_response(self, connection, response, *, role):
        return device_engine.apply_response(connection, response, role=role)

    def device_apply_pull(self, connection, items, *, role, fault=None):
        return device_engine.apply_pull(connection, items, role=role, fault=fault)

    def device_rebaseline(self, connection, *, main_epoch):
        return device_engine.rebaseline_local(connection, main_epoch=main_epoch)


def load_engine(kind: str):
    """reference | app | auto. Raises app_engine.HarnessUnavailable for app."""
    if kind == "reference":
        return ReferenceEngine()
    if kind == "app":
        return app_engine.AppEngine()
    try:
        return app_engine.AppEngine()
    except app_engine.HarnessUnavailable:
        return ReferenceEngine()
