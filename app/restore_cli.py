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
    python -m app.restore_cli --archive "E:\\backups\\myhotel_backup_....zip.enc" [--lang ar|en|zh]

The interface language follows --lang, then the MYHOTEL_LANG environment
variable, and defaults to Arabic like the app. Restoring replaces the entire
current database, so the command asks for typed confirmation first.
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from app.backup_crypto import (
    EncryptionRecordError,
    PassphraseRequiredError,
    WrongPassphraseError,
    has_passphrase,
    is_encrypted_archive,
)
from app.backup_workflow import (
    KEY_FILE_NAME,
    get_backup_root,
    restore_backup,
    save_backup_root,
    sha256_file,
)
from app.db import SessionLocal
from app.i18n import LANGUAGES

_MESSAGES = {
    "archive_not_found": {
        "ar": "النسخة غير موجودة: {path}",
        "en": "Archive not found: {path}",
        "zh": "找不到备份文件:{path}",
    },
    "cannot_configure_root": {
        "ar": "تعذّر ضبط مجلد النسخ الاحتياطي: {error}",
        "en": "Cannot configure the backup location: {error}",
        "zh": "无法配置备份位置:{error}",
    },
    "backup_root_set": {
        "ar": "تم ضبط مجلد النسخ الاحتياطي على {path}",
        "en": "Backup location set to {path}",
        "zh": "备份位置已设置为 {path}",
    },
    "missing_key_file_warning": {
        "ar": "تحذير: ملف مفتاح الاسترجاع {name} غير موجود في {path}. لا يمكن فتح النسخة المشفّرة إلا إذا كانت قاعدة بيانات هذا التثبيت تحتفظ بسجل التشفير.",
        "en": "Warning: the recovery key file {name} is missing from {path}. The archive can only be opened if this installation's database already holds the encryption record.",
        "zh": "警告:{path} 中缺少恢复密钥文件 {name}。仅当此安装的数据库已保存加密记录时,才能打开该备份。",
    },
    "passphrase_prompt": {
        "ar": "كلمة سر التشفير: ",
        "en": "Backup passphrase: ",
        "zh": "备份加密密码:",
    },
    "passphrase_required": {
        "ar": "النسخة مشفّرة وتحتاج كلمة سر التشفير.",
        "en": "A passphrase is required for an encrypted archive.",
        "zh": "加密的备份文件需要加密密码。",
    },
    "about_to_restore": {
        "ar": "سيتم الآن استعادة {name}",
        "en": "About to restore {name}",
        "zh": "即将恢复 {name}",
    },
    "replaces_warning": {
        "ar": "هذا يستبدل قاعدة البيانات الحالية بالكامل. تُنشأ أولًا نسخة احتياطية مُتحقق منها قبل الاستعادة.",
        "en": "This REPLACES the entire current database. A verified pre-restore backup is created first.",
        "zh": "这将替换当前整个数据库。系统会先创建经过验证的恢复前备份。",
    },
    "confirm_prompt": {
        "ar": "اكتب RESTORE للتأكيد: ",
        "en": "Type RESTORE to continue: ",
        "zh": "输入 RESTORE 以继续:",
    },
    "cancelled": {
        "ar": "أُلغيت العملية؛ لم يتغير شيء.",
        "en": "Cancelled; nothing was changed.",
        "zh": "已取消;未做任何更改。",
    },
    "wrong_passphrase": {
        "ar": "كلمة سر التشفير غير صحيحة.",
        "en": "The backup passphrase is incorrect.",
        "zh": "备份加密密码不正确。",
    },
    "encryption_record_damaged": {
        "ar": "سجل التشفير المخزّن تالف؛ توقفت العملية دون إنتاج نسخة غير مشفّرة.",
        "en": "The stored encryption record is damaged; the operation stopped without producing an unencrypted archive.",
        "zh": "存储的加密记录已损坏;操作已停止,未生成未加密的备份。",
    },
    "restore_failed": {
        "ar": "فشلت الاستعادة: {error}",
        "en": "Restore failed: {error}",
        "zh": "恢复失败:{error}",
    },
    "passphrase_not_configured": {
        "ar": "النسخ الاحتياطية الجديدة مشفّرة إجباريًا. عيّن كلمة سر التشفير أولًا من إعدادات النسخ الاحتياطي في البرنامج، ثم أعد المحاولة.",
        "en": "New backups must be encrypted. Set the backup passphrase in the app's backup settings first, then retry.",
        "zh": "新备份必须加密。请先在应用的备份设置中设置加密密码,然后重试。",
    },
    "restore_done": {
        "ar": "اكتملت الاستعادة وتم التحقق منها.",
        "en": "Restore completed and verified.",
        "zh": "恢复已完成并通过验证。",
    },
    "invalid_language": {
        "ar": "لغة غير مدعومة: {value}. الخيارات: ar, en, zh",
        "en": "Unsupported language: {value}. Choices: ar, en, zh",
        "zh": "不支持的语言:{value}。可选:ar, en, zh",
    },
}


def _msg(key: str, lang: str, **kwargs: object) -> str:
    return _MESSAGES[key].get(lang, _MESSAGES[key]["en"]).format(**kwargs)


def _resolve_language(cli_value: str | None) -> str:
    candidate = (cli_value or os.environ.get("MYHOTEL_LANG") or "ar").strip().lower()
    return candidate if candidate in LANGUAGES else ""


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
    parser.add_argument(
        "--lang",
        choices=sorted(LANGUAGES),
        help="Interface language (default: MYHOTEL_LANG or ar).",
    )
    args = parser.parse_args(argv)

    lang = _resolve_language(args.lang)
    if not lang:
        print(_msg("invalid_language", "en", value=args.lang or os.environ.get("MYHOTEL_LANG")))
        return 2

    archive = Path(args.archive).expanduser().resolve()
    if not archive.is_file():
        print(_msg("archive_not_found", lang, path=archive))
        return 2

    _apply_migrations()

    # New backups are always encrypted, and every restore begins with a new
    # pre-restore backup — so a passphrase must be configured before restoring.
    with SessionLocal() as db:
        if not has_passphrase(db):
            print(_msg("passphrase_not_configured", lang))
            return 2

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
                print(_msg("cannot_configure_root", lang, error=exc))
                return 2
            db.commit()
            print(_msg("backup_root_set", lang, path=backup_root))

    passphrase: str | None = None
    if is_encrypted_archive(archive):
        key_file = backup_root / KEY_FILE_NAME
        if not key_file.is_file():
            print(_msg("missing_key_file_warning", lang, name=KEY_FILE_NAME, path=backup_root))
        passphrase = getpass.getpass(_msg("passphrase_prompt", lang))
        if not passphrase:
            print(_msg("passphrase_required", lang))
            return 2

    print(_msg("about_to_restore", lang, name=archive.name))
    print(_msg("replaces_warning", lang))
    confirmation = input(_msg("confirm_prompt", lang))
    if confirmation != "RESTORE":
        print(_msg("cancelled", lang))
        return 1

    try:
        result = restore_backup(
            archive,
            sha256_file(archive),
            actor=args.actor,
            reason=args.reason,
            passphrase=passphrase,
        )
    except WrongPassphraseError:
        print(_msg("wrong_passphrase", lang))
        return 1
    except EncryptionRecordError:
        print(_msg("encryption_record_damaged", lang))
        return 1
    except (PassphraseRequiredError, ValueError, RuntimeError, OSError) as exc:
        print(_msg("restore_failed", lang, error=exc))
        return 1

    print(_msg("restore_done", lang))
    if isinstance(result, dict):
        for key, value in result.items():
            print(f"  {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
