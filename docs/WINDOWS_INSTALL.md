# Windows 10 / Windows 11 installation and first administrator

This is the initial setup path for the local M.Y. Business Hotel HR application. It does not install a Windows service or configure auto-start.

## Prerequisites
1. Windows 10 or Windows 11.
2. Python 3.11 with the Python Launcher for Windows (the setup script invokes `py -3.11`).
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

The application binds only to 127.0.0.1. Do not expose it publicly.

## Automatic backup schedule (separate step)
Setup does NOT schedule automatic backups. After the first installation, and after the backup folder is set inside the app (Backup & Restore page), run once in PowerShell:
   .\scripts\windows\install_backup_tasks.ps1
This installs the Windows scheduled tasks (daily 02:00, weekly Sunday 02:30, quarterly 03:00). Verify afterwards: open Task Scheduler and confirm the M.Y. Business Hotel backup tasks exist and are enabled, then create one manual backup from the app and confirm it appears as a verified success in the backup history. Until these tasks are installed and verified, backups are manual only.

## Backup encryption
Set the backup encryption passphrase from the app's Backup & Restore page before relying on backups. New backups are then stored encrypted (.zip.enc) and restore asks for the passphrase. Keep the passphrase safe: losing it makes encrypted backups unrecoverable. The recovery key file backup_encryption_key.json written beside the backups lets a fresh reinstall unlock them with the passphrase; do not delete it.

## Backup and restore acceptance
Before production use, verify backup creation, SHA-256/integrity validation, retention behavior, restore confirmation and audit, automatic pre-restore backup, successful restore verification, and rollback after an intentionally failed restore in a disposable test database.

## Troubleshooting
- .env contains database credentials and the session secret. Never commit or share it.
- If database connection fails, verify PostgreSQL is running and the database credentials are correct.
- If port 8000 is in use, stop the other local process.
- /health reports database connectivity only; it does not certify release readiness.

## Release limitation
This branch is feature-complete for local testing, but it is not production-accepted until the final acceptance gates in docs/PROJECT_HANDOFF.md are executed. Use synthetic records only until then.
