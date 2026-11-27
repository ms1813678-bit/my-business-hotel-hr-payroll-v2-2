"""One sync instance as a process: the main device or one of the two devices.

    python -m tests.sync_harness.server --role main --db-url <url> --port 55432
    python -m tests.sync_harness.server --role device --device-id device-b \
        --role-name assistant_manager --db-url <url> --port 55433 --main-url http://127.0.0.1:55432

The process owns exactly one PostgreSQL database and one HTTP port, which is
what the design note describes. The main device serves /sync/session and
/sync/confirm; a device serves /harness/sync, which performs one whole session
(send, apply, confirm) against the main device. /harness/* is the test surface
the scenarios drive: simulated screen edits, the administrator's conflict
decisions, state introspection and the fault hook.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import threading
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import contract


class Faults:
    """Arms one crash point; reaching it exits the process without cleanup."""

    def __init__(self, point: str | None = None):
        if point is not None and point not in contract.FAULT_POINTS:
            raise SystemExit(f"unknown fault point {point!r}")
        self.armed = point
        self.lock = threading.Lock()

    def arm(self, point: str) -> None:
        if point not in contract.FAULT_POINTS:
            raise ValueError(f"unknown fault point {point!r}")
        with self.lock:
            self.armed = point

    def hit(self, point: str) -> None:
        with self.lock:
            if self.armed != point:
                return
            self.armed = None
        sys.stderr.write(f"SYNC_HARNESS_FAULT_HIT {point}\n")
        sys.stderr.flush()
        os._exit(contract.FAULT_EXIT_CODE)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tests.sync_harness.server")
    parser.add_argument("--role", required=True, choices=("main", "device"))
    parser.add_argument("--db-url", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--device-id", default="")
    parser.add_argument("--role-name", default="system_admin")
    parser.add_argument("--main-url", default="")
    parser.add_argument("--clock-offset-seconds", type=int, default=0)
    parser.add_argument("--engine", default="auto", choices=("auto", "reference", "app"))
    parser.add_argument("--fault", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # The product's settings read these at import time; each process is one instance.
    os.environ.setdefault("DATABASE_URL", args.db_url)
    os.environ.setdefault("SESSION_SECRET", "sync-harness-only-session-secret-0123456789")
    os.environ.setdefault("APP_ENV", "test")
    os.environ["DATABASE_URL"] = args.db_url

    from . import client, device_engine, engines, reference_engine, store

    class Instance:
        def __init__(self) -> None:
            self.engine = store.make_engine(args.db_url)
            store.ensure_harness_schema(self.engine)
            self.faults = Faults(args.fault)
            self.lock = threading.Lock()
            self.role = args.role
            self.device_id = args.device_id
            self.role_name = args.role_name
            self.clock_offset = args.clock_offset_seconds
            self.impl = engines.load_engine(args.engine)
            self.engine_kind = self.impl.name
            if self.role == "device":
                with self.engine.begin() as connection:
                    if store.meta_get(connection, "device_id") is None:
                        device_engine.initialise(connection, device_id=self.device_id,
                                                 role=self.role_name, epoch=None)

        def _make_impl_unused(self):
            return None

        # -- introspection

        def health(self) -> dict:
            with self.engine.connect() as connection:
                epoch = reference_engine.read_epoch(connection)
                counter = store.current_counter(connection) if self.role == "main" else None
            return {"ok": True, "role": self.role, "device_id": self.device_id,
                    "role_name": self.role_name, "pid": os.getpid(),
                    "port": self.http.server_address[1], "engine": self.engine_kind,
                    "epoch": epoch, "counter": counter, "fault": self.faults.armed,
                    "clock_offset": self.clock_offset}

        def state(self) -> dict:
            with self.engine.connect() as connection:
                if self.role == "main":
                    return {
                        "role": "main",
                        "counter": store.current_counter(connection),
                        "epoch": reference_engine.read_epoch(connection),
                        "devices": [dict(row) for row in connection.execute(
                            reference_engine.text("SELECT * FROM harness_sync_device ORDER BY device_id")
                        ).mappings()],
                        "conflicts": [dict(row) for row in connection.execute(
                            reference_engine.text("SELECT * FROM harness_sync_conflict ORDER BY id")
                        ).mappings()],
                        "alerts": [dict(row) for row in connection.execute(
                            reference_engine.text("SELECT * FROM harness_sync_alert ORDER BY id")
                        ).mappings()],
                        "incomplete": reference_engine._incomplete(connection),
                        "sessions": [dict(row) for row in connection.execute(
                            reference_engine.text("SELECT session_id, device_id, confirmed_at "
                                                  "FROM harness_sync_session ORDER BY created_at")
                        ).mappings()],
                    }
                return {
                    "role": "device",
                    "device": device_engine.pending_state(connection),
                    "outbox": [dict(row) for row in connection.execute(
                        reference_engine.text("SELECT seq, op, entity, uid, field, state "
                                              "FROM harness_sync_outbox ORDER BY seq")
                    ).mappings()],
                    "conflicts": [dict(row) for row in connection.execute(
                        reference_engine.text("SELECT id, kind, entity, uid, field, device_seq, status "
                                              "FROM harness_sync_conflict ORDER BY id")
                    ).mappings()],
                    "last_refusal": json.loads(store.meta_get(connection, "last_refusal") or "null"),
                }

        # -- work

        def local_change(self, payload: dict) -> dict:
            if self.role == "main":
                with self.engine.begin() as connection:
                    return self.impl.local_change(
                        connection, op=payload["op"], entity=payload["entity"],
                        uid=payload["uid"], field=payload.get("field", ""),
                        value=payload.get("value"))
            when = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=self.clock_offset)
            with self.engine.begin() as connection:
                return self.impl.device_queue_change(
                    connection, op=payload["op"], entity=payload["entity"], uid=payload["uid"],
                    field=payload.get("field", ""), value=payload.get("value"), local_time=when)

        def apply_session(self, payload: dict) -> dict:
            if self.role != "main":
                raise ValueError("only the main device applies sessions")
            try:
                request = contract.validate_session(payload)
            except contract.ContractError as exc:
                return contract.refusal("refused_protocol", f"Malformed session: {exc}")
            with self.lock:
                self.faults.hit("main_after_validate")
                with self.engine.begin() as connection:
                    response = self.impl.apply_session(
                        connection, request, session_id=request["session_id"],
                        fault=self.faults.hit)
                    self.faults.hit("main_before_commit")
                return response

        def confirm(self, payload: dict) -> dict:
            if self.role != "main":
                raise ValueError("only the main device records confirmations")
            with self.lock, self.engine.begin() as connection:
                self.impl.confirm(connection, session_id=payload["session_id"],
                                  device_id=payload["device_id"],
                                  applied_counter=int(payload["applied_counter"]))
            return {"ok": True}

        def resolve(self, payload: dict) -> dict:
            with self.lock, self.engine.begin() as connection:
                return self.impl.resolve_conflict(
                    connection, conflict_id=int(payload["conflict_id"]),
                    decision=payload["decision"])

        def complete(self, payload: dict) -> dict:
            with self.lock, self.engine.begin() as connection:
                return self.impl.complete_employee(
                    connection, uid=payload["uid"], values=payload["values"])

        def rebaseline(self, payload: dict) -> dict:
            with self.lock, self.engine.begin() as connection:
                return self.impl.rebaseline_device(connection, device_id=payload["device_id"])

        def pair(self, payload: dict) -> dict:
            with self.lock, self.engine.begin() as connection:
                reference_engine.pair_device(connection, payload["device_id"], payload["role"],
                                             epoch=reference_engine.read_epoch(connection))
            return {"ok": True, "device_id": payload["device_id"], "role": payload["role"]}

        def device_rebaseline(self, payload: dict) -> dict:
            with self.engine.begin() as connection:
                return self.impl.device_rebaseline(connection,
                                                   main_epoch=payload.get("main_epoch"))

        def sync(self, payload: dict) -> dict:
            if self.role != "device":
                raise ValueError("only a device starts a session")
            main_url = (payload.get("main_url") or args.main_url).rstrip("/")
            if not main_url:
                raise ValueError("no main device URL")
            with self.lock:
                with self.engine.begin() as connection:
                    request = self.impl.device_build_request(connection,
                                                             session_id=str(uuid.uuid4()))
                self.faults.hit("device_before_send")
                response = client.post_json(main_url + "/sync/session", request, timeout=120.0)
                self.faults.hit("device_during_receive")
                summary = {"status": response.get("status"), "message": response.get("message", ""),
                           "session_id": request["session_id"], "request": request,
                           "response": response}
                if response.get("status") != "ok":
                    with self.engine.begin() as connection:
                        store.meta_set(connection, "last_refusal",
                                       json.dumps({"status": response.get("status"),
                                                   "message": response.get("message", ""),
                                                   "main_epoch": response.get("main_epoch")}))
                    return summary
                with self.engine.begin() as connection:
                    self.impl.device_apply_response(connection, response, role=self.role_name)
                    applied = self.impl.device_apply_pull(connection, response.get("changes", []),
                                                          role=self.role_name, fault=self.faults.hit)
                    # A full-state pull carries counter 0 items; the session's server
                    # counter is the point the device is now level with.
                    applied = max(applied, int(response.get("server_counter") or 0))
                    store.meta_set(connection, "applied_counter", str(applied))
                self.faults.hit("device_before_confirm")
                client.post_json(main_url + "/sync/confirm",
                                 {"protocol": contract.PROTOCOL,
                                  "session_id": request["session_id"],
                                  "device_id": self.device_id,
                                  "applied_counter": applied}, timeout=30.0)
                summary.update({"applied": response.get("applied", []),
                                "duplicate": response.get("duplicate", []),
                                "conflicts": response.get("conflicts", []),
                                "rejected": response.get("rejected", []),
                                "resolved": response.get("resolved", []),
                                "pull": len(response.get("changes", [])),
                                "applied_counter": applied,
                                "code_corrections": response.get("code_corrections", [])})
                with self.engine.begin() as connection:
                    store.meta_set(connection, "last_refusal", "null")
                return summary

    instance = Instance()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *values) -> None:
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % values))

        def _send(self, payload: dict, code: int = 200) -> None:
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            return json.loads(raw.decode("utf-8") or "{}")

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            try:
                if self.path == "/harness/health":
                    return self._send(instance.health())
                if self.path == "/harness/state":
                    return self._send(instance.state())
                return self._send({"error": "not found"}, 404)
            except Exception as exc:  # pragma: no cover - surfaced to the scenario
                traceback.print_exc()
                return self._send({"error": f"{type(exc).__name__}: {exc}"}, 500)

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            routes = {
                "/harness/local-change": instance.local_change,
                "/harness/admin/resolve": instance.resolve,
                "/harness/admin/complete": instance.complete,
                "/harness/admin/rebaseline": instance.rebaseline,
                "/harness/pair": instance.pair,
                "/harness/device-rebaseline": instance.device_rebaseline,
                "/harness/sync": instance.sync,
                "/sync/session": instance.apply_session,
                "/sync/confirm": instance.confirm,
            }
            try:
                payload = self._body()
            except Exception:
                return self._send({"error": "body is not JSON"}, 400)
            if self.path == "/harness/fault":
                try:
                    instance.faults.arm(payload["point"])
                except (KeyError, ValueError) as exc:
                    return self._send({"error": str(exc)}, 400)
                return self._send({"armed": instance.faults.armed})
            if self.path == "/harness/shutdown":
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return self._send({"ok": True})
            handler = routes.get(self.path)
            if handler is None:
                return self._send({"error": "not found"}, 404)
            try:
                return self._send(handler(payload))
            except Exception as exc:
                traceback.print_exc()
                return self._send({"error": f"{type(exc).__name__}: {exc}"}, 500)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.daemon_threads = True
    instance.http = httpd
    port = httpd.server_address[1]
    print(f"SYNC_HARNESS_READY role={args.role} port={port} pid={os.getpid()}", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        instance.engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
