"""Synthetic executable oracle, NOT application synchronization code.

Fixtures stipulate eligibility and authorized reviewer. No real role, device,
permission, network, storage, clock or payroll integration is represented.
"""
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Intent:
    operation: str
    employee: str
    base: int
    value: Decimal


class SalaryModel:
    def __init__(self):
        self.records = {'E1': (1, Decimal('1000.00')), 'E2': (1, Decimal('2000.00'))}
        self.intents = {}
        self.status = {}
        self.version = {}
        self.comparisons = {}
        self.applications = {}
        self.decisions = {}
        self.audit = []
        self.client = {}
        self.authorized = True  # Explicit synthetic test fixture, not a grant.

    def submit(self, intent):
        if not self.authorized:
            return 'unauthorized'
        if not isinstance(intent, Intent) or not intent.value.is_finite():
            return 'invalid'
        if intent.operation in self.intents:
            return self.status[intent.operation] if self.intents[intent.operation] == intent else 'content_mismatch'
        if not self.authorized:
            return 'unauthorized'
        if intent.employee not in self.records:
            return 'missing'
        self.intents[intent.operation] = intent
        self.applications[intent.operation] = 0
        self.status[intent.operation] = 'pending'
        self.version[intent.operation] = 1
        self.comparisons[intent.operation] = self.records[intent.employee][0]
        self.audit.append(('submitted', intent.operation))
        # This oracle models the Mo-decision path only. No unrelated operation
        # approval chain or automatic eligible-write policy is inferred.
        return 'pending'

    def edit_hub(self, employee, value):
        revision, _old = self.records[employee]
        self.records[employee] = (revision + 1, value)

    def decide(self, operation, command, accept, comparison):
        if not self.authorized:
            return 'unavailable'
        if command in self.decisions:
            return self.decisions[command] if self.decisions[command][:3] == (operation, accept, comparison) else 'command_mismatch'
        if operation not in self.intents or not self.authorized:
            return 'unavailable'
        if self.status[operation] in ('accepted', 'cancelled'):
            return self.status[operation]
        intent = self.intents[operation]
        current, _value = self.records[intent.employee]
        if comparison != current or comparison != self.comparisons[operation]:
            self.comparisons[operation] = current
            self.version[operation] += 1
            self.audit.append(('comparison_refreshed', operation))
            return 'refresh_required'
        if accept:
            self.records[intent.employee] = (current + 1, intent.value)
            self.applications[operation] += 1
        self.status[operation] = 'accepted' if accept else 'cancelled'
        self.version[operation] += 1
        result = (operation, accept, comparison, self.status[operation])
        self.decisions[command] = result
        self.audit.append((self.status[operation], operation))
        return result

    def observe(self, operation, version, status):
        old_version, old_status = self.client.get(operation, (0, 'unknown'))
        if old_status in ('accepted', 'cancelled') or version <= old_version:
            return
        self.client[operation] = (version, status)

    def invariant(self):
        for operation, intent in self.intents.items():
            assert self.applications[operation] in (0, 1)
            assert (self.status[operation] == 'accepted') == (self.applications[operation] == 1)
            assert self.status[operation] != 'cancelled' or self.applications[operation] == 0
            assert any(event == ('submitted', intent.operation) for event in self.audit)
