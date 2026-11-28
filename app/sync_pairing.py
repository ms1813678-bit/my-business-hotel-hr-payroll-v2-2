"""One-time pairing code for a new sync device (pure: no database, no network, not wired).

Design: SYNC_DESIGN section 7 (code shown on the main device, typed on the new device) and section 8 proposals
(lifetime proposal 10 minutes, single use). The lifetime and the failure limit are REQUIRED arguments with no
default: they are not decided.

- new_code(): 8 characters from an alphabet without look-alikes (no 0/O/1/I/L), shown as XXXX-XXXX. About 40 bits,
  so it is only safe with a short lifetime and a failure limit; both are enforced by check_code.
- Only a salted scrypt hash is kept, never the code. The record is immutable; check_code returns the next record and
  the caller stores it in the same transaction as whatever the result allows.
- CONCURRENCY (caller's duty): read the stored row with SELECT ... FOR UPDATE and write the returned next record in the
  same transaction. Without that, two parallel attempts both read the old record, lose each other's update and bypass
  the failure lockout and the single use.
- A new issue() must invalidate every older active code of the same pairing slot (the caller marks them used or deletes
  them in the same transaction); this module only knows one record.
- issue(code=...) exists for tests only; production always lets new_code() choose.
- scrypt cost: scrypt_n defaults to 2**14 (fast, below OWASP's current advice). Option proposed for the hotel PCs:
  2**16 (about 200 ms per check, 64 MB). The cost used is stored in the record so old records keep verifying.
- Results: ok | wrong | expired | used | locked. After "ok" the record is used. A wrong code counts a failure; at the
  limit the record is locked and even the right code is refused.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # 31 characters
CODE_LEN = 8
OK, WRONG, EXPIRED, USED, LOCKED = "ok", "wrong", "expired", "used", "locked"


class PairingError(ValueError):
    pass


@dataclass(frozen=True)
class PairingRecord:
    salt: str  # hex
    code_hash: str  # hex
    expires_at: str  # canonical UTC
    used: bool = False
    failures: int = 0
    scrypt_n: int = 2 ** 14


def _utc(value, label):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PairingError(label + " must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _stamp(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _posint(value, label):
    if type(value) is not int or value < 1:
        raise PairingError(label + " must be a positive integer")
    return value


def normalize(code):
    if not isinstance(code, str):
        return ""
    return "".join(ch for ch in code.upper() if ch not in "- \t")


def _check_n(n):
    if type(n) is not int or n < 2 ** 14 or n > 2 ** 17 or n & (n - 1):
        raise PairingError("scrypt_n must be a power of two from 2**14 to 2**17")
    return n


def _hash(code, salt_hex, n):
    try:  # n is capped at 2**17 (128 MiB), well inside the 256 MiB limit below
        return hashlib.scrypt(code.encode("ascii"), salt=bytes.fromhex(salt_hex), n=n, r=8, p=1, dklen=32,
                              maxmem=256 * 1024 * 1024).hex()
    except (ValueError, MemoryError):
        raise PairingError("scrypt could not run with these settings") from None


def _validate(record):
    """A record read back from storage is untrusted: refuse corrupt fields instead of guessing."""
    if not isinstance(record, PairingRecord):
        raise PairingError("record required")
    try:
        if len(record.salt) != 32 or len(record.code_hash) != 64 or not isinstance(record.salt, str):
            raise ValueError
        bytes.fromhex(record.salt)
        bytes.fromhex(record.code_hash)
        expires = datetime.strptime(record.expires_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        raise PairingError("record is corrupt") from None
    if type(record.used) is not bool or type(record.failures) is not int or record.failures < 0:
        raise PairingError("record is corrupt")
    _check_n(record.scrypt_n)
    return expires


def new_code():
    raw = "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
    return raw[:4] + "-" + raw[4:]


def issue(now, *, lifetime_minutes, code=None, scrypt_n=2 ** 14):
    """Returns (code to show on the main device, record to store). `code` is for tests only."""
    now = _utc(now, "now")
    _posint(lifetime_minutes, "lifetime_minutes")
    _check_n(scrypt_n)
    shown = code if code is not None else new_code()
    raw = normalize(shown)
    if len(raw) != CODE_LEN or any(ch not in ALPHABET for ch in raw):
        raise PairingError("code has the wrong shape")
    salt = secrets.token_hex(16)
    return shown, PairingRecord(salt, _hash(raw, salt, scrypt_n), _stamp(now + timedelta(minutes=lifetime_minutes)), scrypt_n=scrypt_n)


def check_code(record, typed, now, *, max_failures):
    """Returns (result, next_record). Never raises for a wrong or malformed typed code."""
    expires = _validate(record)
    now = _utc(now, "now")
    _posint(max_failures, "max_failures")
    if record.used:
        return USED, record
    if record.failures >= max_failures:
        return LOCKED, record
    if now >= expires:
        return EXPIRED, record
    raw = normalize(typed)
    good = len(raw) == CODE_LEN and all(ch in ALPHABET for ch in raw)
    match = good and hmac.compare_digest(_hash(raw, record.salt, record.scrypt_n), record.code_hash)
    if match:
        return OK, replace(record, used=True)
    return WRONG, replace(record, failures=record.failures + 1)
