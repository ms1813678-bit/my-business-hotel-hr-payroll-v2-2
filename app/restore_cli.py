"""Disaster-recovery restore entry point for a fresh install.

The web UI restores backups listed in the database (BackupRun rows). After a
total loss the fresh database is empty, so there is nothing to select. This
command restores an archive file directly from the backup drive: it applies
any pending schema migrations, configures the backup location when missing,
asks for the passphrase when the archive is encrypted, and runs the same
verified, rollback-protected restore as the UI. When the fresh database has
no encryption record, the restore unlocks the archive with the passphrase via
the backup_encryption_key.json recovery file kept next to the backups.

Usage:
    python -m app.restore_cli --archive "E:\\backups\\myhotel_backup_....zip.enc"

Restoring replaces the entire current database, so the command asks for typed
confirmation before touching anything.
"""
from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from app.backup_crypto import is_encrypted_archive
from app.backup_workflow import (
    KEY_FILE_NAME,
    get_backup_root,
    restore_backup,
    save_backup_root,
    sha256_file,
)
from app.db import SessionLocal


def _apply_migrations() -> None:
    from scripts.initialize_database import main as initialize_database

    initialize_database()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.restore_cli",
        description="Restore a backup archive directly from the backup drive (fresh-install disaster recovery).",
    )
    parser.add_argument("--archive", required=True, help="Full path of the backup archive to restore.")
    parser.add_argument(
        "--backup-root",
        help="Backup drive folder. Defaults to the archive's own folder; saved when not configured yet.",
    )
    parser.add_argument("--reason", default="Fresh-install disaster recovery", help="Restore reason for the audit log.")
    parser.add_argument("--actor", default="recovery-cli", help="Name recorded in the audit log.")
    args = parser.parse_args(argv)

    archive = Path(args.archive).expanduser().resolve()
    if not archive.is_file():
        print(f"Archive not found: {archive}")
        return 2

    _apply_migrations()

    if args.backup_root:
        backup_root = Path(args.backup_root).expanduser().resolve()
    elif archive.parent.name in {"daily", "weekly", "quarterly", "manual", "pre_restore"}:
        # Archives live under <backup_root>/<backup_type>/.
        backup_root = archive.parent.parent
    else:
        backup_root = archive.parent
    with SessionLocal() as db:
        if get_backup_root(db) is None:
            try:
                save_backup_root(db, str(backup_root), args.actor)
            except ValueError as exc:
                print(f"Cannot configure the backup location: {exc}")
                return 2
            db.commit()
            print(f"Backup location set to {backup_root}")

    passphrase: str | None = None
    if is_encrypted_archive(archive):
        key_file = backup_root / KEY_FILE_NAME
        if not key_file.is_file():
            print(
                f"Warning: the recovery key file {KEY_FILE_NAME} is missing from {backup_root}. "
                "The archive can still be opened only if this installation's database already "
                "holds the encryption record."
            )
        passphrase = getpass.getpass("Backup passphrase: ")
        if not passphrase:
            print("A passphrase is required for an encrypted archive.")
            return 2

    print(f"About to restore {archive.name}")
    print("This REPLACES the entire current database. A verified pre-restore backup is created first.")
    confirmation = input("Type RESTORE to continue: ")
    if confirmation != "RESTORE":
        print("Cancelled; nothing was changed.")
        return 1

    try:
        result = restore_backup(
            archive,
            sha256_file(archive),
            actor=args.actor,
            reason=args.reason,
            passphrase=passphrase,
        )
    except Exception as exc:
        print(f"Restore failed: {exc}")
        return 1

    print("Restore completed and verified.")
    if isinstance(result, dict):
        for key, value in result.items():
            print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
