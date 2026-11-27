"""HTTP client used by the harness scenarios and by a device process."""

from __future__ import annotations

import json
import urllib.error
import urllib.request


class HttpError(RuntimeError):
    def __init__(self, url: str, status: int, body: str):
        super().__init__(f"{url} -> HTTP {status}: {body[:800]}")
        self.url = url
        self.status = status
        self.body = body


def _request(url: str, payload: dict | None, timeout: float, method: str) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raise HttpError(url, exc.code, exc.read().decode("utf-8", "replace")) from None
    if not body:
        return {}
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise HttpError(url, 200, f"not JSON: {body[:400]}") from exc


def get_json(url: str, *, timeout: float = 10.0) -> dict:
    return _request(url, None, timeout, "GET")


def post_json(url: str, payload: dict | None = None, *, timeout: float = 30.0) -> dict:
    return _request(url, payload or {}, timeout, "POST")


class HarnessClient:
    """Control surface of one instance process (main or device)."""

    def __init__(self, base_url: str, *, timeout: float = 30.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payload: dict | None = None, timeout: float | None = None) -> dict:
        return post_json(self.base_url + path, payload, timeout=timeout or self.timeout)

    def health(self) -> dict:
        return get_json(self.base_url + "/harness/health", timeout=5.0)

    def state(self) -> dict:
        return get_json(self.base_url + "/harness/state", timeout=self.timeout)

    def local_change(self, *, op: str, entity: str, uid: str, field: str = "",
                     value=None) -> dict:
        return self._post("/harness/local-change",
                          {"op": op, "entity": entity, "uid": uid, "field": field, "value": value})

    def resolve_conflict(self, conflict_id: int, decision: str) -> dict:
        return self._post("/harness/admin/resolve",
                          {"conflict_id": conflict_id, "decision": decision})

    def complete_employee(self, uid: str, values: dict) -> dict:
        return self._post("/harness/admin/complete", {"uid": uid, "values": values})

    def rebaseline(self, device_id: str) -> dict:
        return self._post("/harness/admin/rebaseline", {"device_id": device_id})

    def pair(self, device_id: str, role: str) -> dict:
        return self._post("/harness/pair", {"device_id": device_id, "role": role})

    def arm_fault(self, point: str) -> dict:
        return self._post("/harness/fault", {"point": point})

    def sync(self, main_url: str, *, timeout: float | None = None) -> dict:
        return self._post("/harness/sync", {"main_url": main_url},
                          timeout=timeout or max(self.timeout, 60.0))

    def device_rebaseline(self, main_epoch: str | None) -> dict:
        return self._post("/harness/device-rebaseline", {"main_epoch": main_epoch})

    def shutdown(self) -> dict:
        return self._post("/harness/shutdown", {})
