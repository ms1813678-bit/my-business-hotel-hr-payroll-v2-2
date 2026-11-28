"""Deterministic sync uids for rows every install creates itself (pure: no database, no migration).

A reference row made by a migration or seed (a leave type such as "annual", a branch) would otherwise get a
different random uid on each install. Here the uid is derived from a fixed namespace, the entity and the
natural key, so all installs agree. The result is a canonical lowercase UUID4-shaped string (version and
variant bits forced), so it passes app.sync_uid_mixin.canonical_uid and the database CHECK.

Changing SEED_NAMESPACE or the key rules changes every derived uid: never do it after release.
Natural keys are compared exactly after NFC normalisation and strip; case is kept.
Not wired anywhere: applying these to existing rows needs a migration that is not part of this change.
"""
from __future__ import annotations

import hashlib
import unicodedata

from app.sync_uid_mixin import SyncUidError, canonical_uid

SEED_NAMESPACE = "my-business-hotel/sync-uid/v1"
SEEDED_ENTITIES = frozenset({"leave_types"})  # reference rows created by migrations (SYNC_DESIGN part 1b proposal 1)
# PROPOSAL ONLY, not in SYNC_DESIGN as decided: departments are user-entered, so two installs that each created
# "Front" would silently get the same uid and merge. Branches have no uid in the product yet. Kept so the shape
# can be reviewed; derive_uid refuses them unless proposal=True is passed explicitly.
PROPOSED_ENTITIES = frozenset({"departments", "branches"})


def _part(value, label):
    if not isinstance(value, str):
        raise SyncUidError(label + " must be text")
    text = unicodedata.normalize("NFC", value).strip()
    if not text or "\x00" in text or "\x1f" in text:
        raise SyncUidError(label + " must be non-empty text without control separators")
    return text


def derive_uid(entity, *key_parts, proposal=False):
    if entity not in SEEDED_ENTITIES and not (proposal and entity in PROPOSED_ENTITIES):
        raise SyncUidError("entity has no seeded uid rule")
    if not key_parts:
        raise SyncUidError("a natural key is required")
    parts = [SEED_NAMESPACE, entity] + [_part(p, "key part") for p in key_parts]
    digest = bytearray(hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x40
    digest[8] = (digest[8] & 0x3F) | 0x80
    h = digest.hex()
    return canonical_uid(f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}")


def leave_type_uid(code):
    """Natural key: leave_types.code (unique)."""
    return derive_uid("leave_types", code)


def department_uid(name):
    """PROPOSAL, not decided. Natural key: departments.name (user-entered, so installs may collide)."""
    return derive_uid("departments", name, proposal=True)


def branch_uid(department_uid_value, name):
    """PROPOSAL, not decided. Natural key: (department uid, branches.name), the table's unique pair. Never the integer id."""
    return derive_uid("branches", canonical_uid(department_uid_value), name, proposal=True)
