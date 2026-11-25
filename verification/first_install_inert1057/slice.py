"""Stopped-only synthetic fixture source. Not product installer, not executed.
No public CLI, arbitrary paths, service starts, shortcuts, DB/TLS/network effects.
"""
from dataclasses import dataclass
from enum import Enum
import hashlib
import re

PAYLOAD = {"payload.txt": b"HOnY synthetic inert payload 1045\n",
           "manifest.json": b'{"synthetic":true,"ready":false}\n'}
SDDL = "O:BAG:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"

class Kind(Enum):
    NO_EFFECT = "no-effect"
    OWNED = "owned"
    UNKNOWN = "unknown"

@dataclass(frozen=True)
class Result:
    kind: Kind
    token: object = None
    detail: str = ""

class Refused(RuntimeError):
    pass

class Slice:
    """Native adapter is fixed by reviewed launcher; fake adapter only for tests.
    Returns staged-not-ready. Always rolls back the disposable source fixture.
    """
    def __init__(self, adapter):
        self.a = adapter
        self.owned = []
        self.unknown = False
        self.done = False
        self.events = []

    def effect(self, operation, create):
        self.events.append((operation, "intent"))
        try:
            result = create()
        except BaseException:
            self.unknown = True
            raise
        if type(result) is not Result:
            self.unknown = True
            raise Refused("malformed operation result")
        if result.kind is Kind.OWNED:
            if not self.a.issued(result.token, operation):
                self.unknown = True
                raise Refused("unissued or wrong-operation token")
            self.owned.append((operation, result.token))
            self.events.append((operation, "owned"))
            if result.detail:
                raise Refused(result.detail)
            return result.token
        if result.kind is Kind.UNKNOWN:
            self.unknown = True
        raise Refused(result.kind.value + ": " + result.detail)

    def run(self):
        if self.events or self.done:
            raise Refused("one-shot fixture")
        self.a.preflight()
        root = self.effect("root", self.a.create_root)
        self.a.journal(root, self.events)
        for name, data in PAYLOAD.items():
            self.a.journal(root, self.events + [(name, "intent")])
            token = self.effect(name, lambda: self.a.create_file(root, name, data))
            self.a.verify_file(token, hashlib.sha256(data).hexdigest())
            self.a.journal(root, self.events)
        self.a.journal(root, self.events + [("service", "intent")])
        service = self.effect("service", lambda: self.a.create_service(root))
        self.a.verify_service(service)
        self.a.journal(root, self.events + [("state", "staged-not-ready")])
        return "staged-not-ready"

    def rollback(self):
        if self.done:
            return []
        failures = []
        # A failed/unknown service removal pins release. Never start or stop it.
        service_safe = not self.unknown
        for operation, token in reversed(self.owned):
            if operation != "service":
                continue
            try:
                self.a.remove(token)
                service_safe = True if not self.unknown else False
            except BaseException as exc:
                failures.append(type(exc).__name__)
                service_safe = False
        if service_safe:
            for operation, token in reversed(self.owned):
                if operation == "service":
                    continue
                try:
                    self.a.remove(token)
                except BaseException as exc:
                    failures.append(type(exc).__name__)
                    # A child failure pins root, but independent other files continue.
                    if operation != "root":
                        self.a.pin_root()
        else:
            failures.append("release-preserved-unresolved-service-or-effect")
        if not failures and not self.unknown:
            self.done = True
        return failures


def fixture_id(value):
    if not re.fullmatch(r"[0-9a-f]{32}", value):
        raise Refused("32 lowercase hex fixture identifier required")
    return value
