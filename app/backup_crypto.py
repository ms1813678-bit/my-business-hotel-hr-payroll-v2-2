"""Passphrase-protected backup encryption (AES-256-GCM with a scrypt-wrapped data key).

Design: a random 256-bit data key encrypts every backup archive, so scheduled
unattended backups keep working without anyone typing a passphrase. The data
key is stored in the local database and also wrapped (encrypted) with a key
derived from the owner's passphrase via scrypt. In a disaster the database is
gone; the passphrase alone unwraps the data key and opens the backups. If the
passphrase is forgotten, encrypted backups cannot be recovered.
"""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy.orm import Session

from app.models import SystemSetting

MAGIC = b"MYHENC01"
CHUNK_SIZE = 4 * 1024 * 1024
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 32
SALT_LEN = 16
NONCE_BASE_LEN = 8
END_AAD = b"MYH-END"
SETTING_KEY = "backup_encryption"
MIN_PASSPHRASE_LENGTH = 8
ENCRYPTED_SUFFIX = ".zip.enc"


class WrongPassphraseError(ValueError):
    """The supplied passphrase does not match the configured backup passphrase."""


class PassphraseRequiredError(ValueError):
    """The selected backup archive is encrypted and needs its passphrase."""


class EncryptionRecordError(ValueError):
    """The stored backup encryption record exists but is unreadable.

    Raised so backup and restore paths fail closed: a damaged record must
    never silently degrade an encrypted installation into plaintext archives.
    """


def is_encrypted_archive(path: str | Path) -> bool:
    candidate = Path(path)
    if candidate.name.endswith(ENCRYPTED_SUFFIX):
        return True
    try:
        with candidate.open("rb") as stream:
            return stream.read(len(MAGIC)) == MAGIC
    except OSError:
        return False


def _derive_wrap_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_LEN,
        # 128 * n * r is 32 MiB; OpenSSL's default cap rejects it, so allow 64 MiB.
        maxmem=64 * 1024 * 1024,
    )


def _load_record(db: Session, *, strict: bool = False) -> dict | None:
    setting = db.get(SystemSetting, SETTING_KEY)
    if setting is None or not (setting.value or "").strip():
        return None
    record = None
    try:
        record = json.loads(setting.value)
    except json.JSONDecodeError:
        record = None
    if not isinstance(record, dict) or record.get("version") != 1:
        if strict:
            raise EncryptionRecordError(
                "Backup encryption settings are damaged; refusing to continue without them."
            )
        return None
    return record


def has_passphrase(db: Session) -> bool:
    """True when any encryption record is stored, even a damaged one.

    Presence (not parseability) is the signal: backup paths must fail closed
    on a damaged record rather than silently produce plaintext archives.
    """
    setting = db.get(SystemSetting, SETTING_KEY)
    return setting is not None and bool((setting.value or "").strip())


def load_data_key(db: Session) -> bytes | None:
    """Return the plaintext data key from the local database, when configured.

    Raises EncryptionRecordError when a record exists but is damaged, so
    callers fail closed instead of falling back to unencrypted output.
    """
    record = _load_record(db, strict=True)
    if record is None:
        return None
    try:
        return bytes.fromhex(record["data_key"])
    except (KeyError, ValueError) as exc:
        raise EncryptionRecordError(
            "Backup encryption settings are damaged; refusing to write an unencrypted archive."
        ) from exc


def _unwrap_data_key(record: dict, passphrase: str) -> bytes:
    try:
        salt = bytes.fromhex(record["kdf_salt"])
        nonce = bytes.fromhex(record["wrap_nonce"])
        wrapped = bytes.fromhex(record["wrapped_key"])
        data_key = bytes.fromhex(record["data_key"])
    except (KeyError, ValueError) as exc:
        raise ValueError("Backup encryption settings are invalid.") from exc
    wrap_key = _derive_wrap_key(passphrase, salt)
    try:
        unwrapped = AESGCM(wrap_key).decrypt(nonce, wrapped, None)
    except InvalidTag as exc:
        raise WrongPassphraseError("كلمة سر التشفير غير صحيحة.") from exc
    if unwrapped != data_key:
        # Consistency guard: the passphrase must unwrap to this install's data key.
        raise WrongPassphraseError("كلمة سر التشفير غير صحيحة.")
    return unwrapped


def export_public_record(db: Session) -> dict | None:
    """The passphrase-wrapped key material without the plaintext data key.

    Written next to the backups so a fresh install can unlock archives with the
    passphrase after the original database is gone. Safe to store beside the
    archives: it only permits an offline brute-force of the passphrase, which
    scrypt makes expensive; it never contains the data key itself.
    """
    record = _load_record(db)
    if record is None:
        return None
    return {key: value for key, value in record.items() if key != "data_key"}


def unwrap_data_key_from_record(record: dict, passphrase: str) -> bytes:
    """Unwrap using an exported public record (disaster recovery without the DB).

    The exported record carries no plaintext data key, so the unwrapped key is
    returned directly without a consistency comparison.
    """
    try:
        salt = bytes.fromhex(record["kdf_salt"])
        nonce = bytes.fromhex(record["wrap_nonce"])
        wrapped = bytes.fromhex(record["wrapped_key"])
    except (KeyError, ValueError) as exc:
        raise ValueError("Backup encryption key file is invalid.") from exc
    wrap_key = _derive_wrap_key(passphrase, salt)
    try:
        return AESGCM(wrap_key).decrypt(nonce, wrapped, None)
    except InvalidTag as exc:
        raise WrongPassphraseError("كلمة سر التشفير غير صحيحة.") from exc


def verify_passphrase(db: Session, passphrase: str) -> bool:
    record = _load_record(db)
    if record is None:
        return False
    try:
        _unwrap_data_key(record, passphrase)
    except WrongPassphraseError:
        return False
    return True


def unwrap_data_key_for_restore(db: Session, passphrase: str) -> bytes:
    record = _load_record(db)
    if record is None:
        raise ValueError("Backup encryption is not configured.")
    return _unwrap_data_key(record, passphrase)


def set_passphrase(
    db: Session,
    passphrase: str,
    actor: str,
    *,
    current_passphrase: str | None = None,
) -> None:
    """Set or change the backup passphrase. Changing re-wraps the same data key,
    so previously encrypted backups stay readable with the new passphrase."""
    if len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise ValueError("passphrase_too_short")
    record = _load_record(db)
    if record is None:
        data_key = secrets.token_bytes(KEY_LEN)
    else:
        if current_passphrase is None:
            raise ValueError("current_passphrase_required")
        data_key = _unwrap_data_key(record, current_passphrase)
    salt = secrets.token_bytes(SALT_LEN)
    wrap_key = _derive_wrap_key(passphrase, salt)
    wrap_nonce = secrets.token_bytes(12)
    wrapped = AESGCM(wrap_key).encrypt(wrap_nonce, data_key, None)
    payload = {
        "version": 1,
        "kdf": "scrypt",
        "kdf_n": SCRYPT_N,
        "kdf_r": SCRYPT_R,
        "kdf_p": SCRYPT_P,
        "kdf_salt": salt.hex(),
        "wrap_nonce": wrap_nonce.hex(),
        "wrapped_key": wrapped.hex(),
        "data_key": data_key.hex(),
    }
    setting = db.get(SystemSetting, SETTING_KEY)
    if setting is None:
        db.add(SystemSetting(key=SETTING_KEY, value=json.dumps(payload), updated_by=actor))
    else:
        setting.value = json.dumps(payload)
        setting.updated_by = actor
        setting.updated_at = datetime.utcnow()
    db.flush()


def _chunk_nonce(base: bytes, index: int) -> bytes:
    return base + index.to_bytes(4, "big")


def encrypt_file(data_key: bytes, source: Path, destination: Path) -> None:
    aes = AESGCM(data_key)
    nonce_base = secrets.token_bytes(NONCE_BASE_LEN)
    index = 0
    with source.open("rb") as reader, destination.open("wb") as writer:
        writer.write(MAGIC)
        writer.write(nonce_base)
        writer.write(CHUNK_SIZE.to_bytes(4, "big"))
        while True:
            chunk = reader.read(CHUNK_SIZE)
            if not chunk:
                break
            writer.write(aes.encrypt(_chunk_nonce(nonce_base, index), chunk, index.to_bytes(8, "big")))
            index += 1
        writer.write(aes.encrypt(_chunk_nonce(nonce_base, index), b"", END_AAD + index.to_bytes(8, "big")))


def decrypt_file(data_key: bytes, source: Path, destination: Path) -> None:
    aes = AESGCM(data_key)
    with source.open("rb") as reader, destination.open("wb") as writer:
        magic = reader.read(len(MAGIC))
        if magic != MAGIC:
            raise ValueError("The file is not an encrypted backup archive.")
        nonce_base = reader.read(NONCE_BASE_LEN)
        if len(nonce_base) != NONCE_BASE_LEN:
            raise ValueError("Encrypted backup header is invalid.")
        chunk_size = int.from_bytes(reader.read(4), "big")
        if not (1024 <= chunk_size <= 64 * 1024 * 1024):
            raise ValueError("Encrypted backup header is invalid.")
        block = chunk_size + 16  # one full encrypted chunk: ciphertext bytes plus its 16-byte GCM tag
        index = 0
        while True:
            segment = reader.read(block)
            if len(segment) == block:
                # A full data chunk; the 16-byte end marker always follows the last one.
                try:
                    plain = aes.decrypt(_chunk_nonce(nonce_base, index), segment, index.to_bytes(8, "big"))
                except InvalidTag as exc:
                    raise ValueError("Encrypted backup integrity check failed.") from exc
                writer.write(plain)
                index += 1
                continue
            # Short final segment: last (possibly empty) data chunk + 16-byte end marker.
            if len(segment) < 16:
                raise ValueError("Encrypted backup archive is truncated or corrupt.")
            chunk_ct, marker = segment[:-16], segment[-16:]
            if chunk_ct:
                try:
                    plain = aes.decrypt(_chunk_nonce(nonce_base, index), chunk_ct, index.to_bytes(8, "big"))
                except InvalidTag as exc:
                    raise ValueError("Encrypted backup integrity check failed.") from exc
                writer.write(plain)
                index += 1
            try:
                aes.decrypt(_chunk_nonce(nonce_base, index), marker, END_AAD + index.to_bytes(8, "big"))
            except InvalidTag as exc:
                raise ValueError("Encrypted backup end marker is missing or corrupt.") from exc
            break
