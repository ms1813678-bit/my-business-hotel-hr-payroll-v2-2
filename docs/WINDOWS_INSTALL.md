> Current verification and source identity: [CURRENT_SOURCE_STATUS.md](CURRENT_SOURCE_STATUS.md). This procedure does not certify a Windows release or hotel-device acceptance. Check current source and actual completed test outcomes before applying it.

# Windows 10 / Windows 11 installation and first administrator

This is the initial setup path for the local M.Y. Business Hotel HR application. It does not install a Windows service or configure auto-start. It is not a complete offline first-install bundle: setup upgrades pip and installs dependencies from package sources, while Python and PostgreSQL must already be installed. Offline runtime after installation is a separate acceptance test.

## Prerequisites
1. Windows 10 or Windows 11.
2. Python3.11 with the Python Launcher for Windows is the application-tested choice. The setup script tries3.11 first, then3.12/3.13 if unavailable. Those fallback versions have dependency-resolution evidence only, not full application/native acceptance.
3. PostgreSQL 16 (or a compatible supported release), installed and running locally.
4. A dedicated PostgreSQL database and database user with schema permissions, bound to this computer only (`localhost`, `127.0.0.1`, or `::1`). Remote database hosts are rejected.
5. The project repository checked out locally.

Do not use a production database until the complete application, security, backup, and restore tests are approved.

## First installation
1. Open the project folder.
2. Open PowerShell in the project folder.
3. Run:
   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
   .\scripts\windows\setup.ps1
4. Setup creates a project-local virtual environment and installs dependencies.
5. Enter PostgreSQL connection details; the password is requested as hidden input. The setup accepts loopback database hosts only and rejects remote database servers.
6. Setup writes .env only when it does not already exist.
7. Setup applies migrations, seeds missing standard departments and configured shift patterns, and creates the first administrator.
8. A desktop shortcut named M.Y. Business Hotel HR is created after successful setup.

## Reports
Reports are generated manually by the administrator and saved locally as Excel or PDF. There is no automatic report email or scheduled report job in the initial release.

## Daily start and stop
- Double-click the M.Y. Business Hotel HR desktop shortcut.
- The browser opens at http://127.0.0.1:8000.
- Keep the PowerShell window open while using the application.
- Press Ctrl+C to stop the application.
- PostgreSQL must be running before starting the application.

The application binds only to127.0.0.1 over HTTP. This launcher does not configure TLS, LAN/mobile access or three-device synchronization. Do not expose it publicly.

## Automatic backup schedule (separate step)
Setup does NOT schedule automatic backups. After the first installation, and after the backup folder is set inside the app (Backup & Restore page), run once in PowerShell:
   .\scripts\windows\install_backup_tasks.ps1
This installs the Windows scheduled tasks (daily 02:00, weekly Sunday 02:30, quarterly 03:00). Verify afterwards: open Task Scheduler and confirm the M.Y. Business Hotel backup tasks exist and are enabled, then create one manual backup from the app and confirm it appears as a verified success in the backup history. Until these tasks are installed and verified, backups are manual only.

## Backup encryption
Set the backup encryption passphrase from the app's Backup & Restore page before relying on backups. New backups are then stored encrypted (.zip.enc) and restore asks for the passphrase. Keep the passphrase safe: losing it makes encrypted backups unrecoverable. The recovery key file backup_encryption_key.json written beside the backups lets a fresh reinstall unlock them with the passphrase; do not delete it.

Changing the passphrase later re-wraps the same data key, so every archive - old and new - stays openable with the new passphrase, and the key file beside the backups is updated automatically. Any older copy of backup_encryption_key.json you kept elsewhere before the change still unlocks the archives with the OLD passphrase: delete such stale copies wherever they exist, exactly as you would destroy a written-down old passphrase.

Restoring an archive created BEFORE a passphrase change also restores the encryption settings stored inside it: the passphrase that was active at that backup's creation becomes the active one again. The data key never changes, so all archives remain openable. Safe next steps after restoring such an archive:

- Immediately change the passphrase on the Backup & Restore page: enter the restored archive's era passphrase as the current one, then your preferred new passphrase. Everything returns to your newest passphrase and the key file refreshes automatically.
- If you no longer remember the archive-era passphrase, preserve the newer recovery key file before restoring. On an accountless disaster restore, the exact newer recovery record that successfully opened the archive can prove possession once at first sign-in, together with the account password. Password-change and recovery-code regeneration restrictions still apply. This does not change the active backup encryption record: it remains the archive-era record. No supported encryption-reset command is shipped. Do not edit database settings or discard any recovery record to work around a forgotten phrase.
- A reset is not a passphrase change: resetting the encryption settings creates a brand-new data key (a new epoch). Future backups are encrypted with the new key, while existing archives stay tied to the old key and open only with their own epoch's passphrase and key file. Never discard an old epoch's key file and passphrase while its archives are still needed.
- Keep each epoch's key file clearly separated: rename the preserved copy with its date (for example backup_encryption_key_2026-11.json) or keep it in a dated folder, never save it over the current file and never write one epoch's file over another - losing your only copy of an epoch's key file means losing the ability to open that epoch's archives. When you actually need to restore an archive from an older epoch, copy that epoch's key file into the backup folder under the expected name backup_encryption_key.json (leaving your separated original untouched), restore with that epoch's passphrase, then put the current key file back.
- If the active passphrase is lost entirely with no suitable key file copy preserved, encrypted backups become unrecoverable, exactly as with a forgotten passphrase.

### Disaster recovery on a fresh PC

After a total loss the new installation's database is empty, so the web page has no backup entries to select. Use the recovery command instead:

1. Install the app and PostgreSQL as above. Before running the recovery command, configure backup encryption on the target installation through Backup & Restore. The command refuses an unconfigured target because it must create an encrypted pre-restore snapshot. A completely unconfigured empty database is not supported by this command. Preserve the source archive recovery record before configuring the target; do not overwrite the only source key file with the target installation's file. Normal setup creates a target administrator. Restoring with that existing account uses current-credential carry-over and archive-only account locks, not the accountless one-use proof branch.
2. Plug in the backup drive and locate the archive (for example `E:\backups\manual\mybusinesshotel_manual_....zip.enc`).
3. Stop the running application with Ctrl+C. Open PowerShell in the project folder and use the interpreter where setup installed the dependencies (activation is not required):
   `& .\.venv\Scripts\python.exe -m app.restore_cli --archive "E:\backups\manual\<archive>.zip.enc"`
   The standard setup script does not provision an accountless recovery target; this command does not create one. The interface language defaults to Arabic; add `--lang en` or `--lang zh` to switch (or set MYHOTEL_LANG).
   The command applies schema migrations, saves the backup location when it is not configured yet, asks for the encryption passphrase (it unlocks the archive through the backup_encryption_key.json file beside the backups), and asks you to type RESTORE before replacing the database - with the same verified pre-restore snapshot and automatic rollback as the web page.
4. For the normal setup path, sign in with the target administrator credentials created before restore. Archive-only accounts remain locked until the current administrator re-enables them and resets their passwords through the existing audited account flows. Re-activation alone is refused. You do not need the archive-era backup phrase merely to sign in on that path. The active backup-encryption record still returns to archive time, so a forgotten archive-era phrase remains a separate limitation for changing backup encryption; no reset command is supplied. Do not delete accounts to try to reach the accountless path. For a separately provisioned accountless target only, sign in with the archive account password and the phrase for the exact recovery record used to open the archive. This possession check is one-use; it does not skip password changes or recovery-code regeneration. Legacy restores without this proof still require the archive-era phrase. Verify employees, attendance, and payroll data.

## Backup and restore acceptance
Before production use, verify backup creation, SHA-256/integrity validation, that no backup is ever deleted automatically, restore confirmation and audit, automatic pre-restore backup, successful restore verification, and rollback after an intentionally failed restore in a disposable test database.

## Troubleshooting
- .env contains database credentials and the session secret. Never commit or share it.
- If database connection fails, verify PostgreSQL is running and the database credentials are correct.
- If port 8000 is in use, stop the other local process.
- /health reports database connectivity only; it does not certify release readiness.

## Release limitation
This branch contains reviewed development preparations, but it is not production-accepted until the final acceptance gates in docs/PROJECT_HANDOFF.md are executed. Use synthetic records only until then.
