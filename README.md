# M.Y. Business Hotel — HR & Payroll System — Version 2.2

**Independent Version 2.2 | M.Y. Business Hotel — Maadi, Cairo**

A local-first HR and payroll application for Windows 10/11, using PostgreSQL on the hotel computer. Version 2 is developed in this repository only. The previous repository is a read-only reference and must not be modified as part of V2 work.

## Product commitments

- One account only: HR Manager / System Administrator.
- Arabic (RTL), English (LTR), and Simplified Chinese (LTR).
- Local PostgreSQL and local file storage. No cloud runtime.
- Core HR/payroll workflows continue when the local AI model is unavailable.
- The intelligent assistant is a first-class system component: cross-module read/analysis, proactive checks, and controlled execution.
- Every write command follows prepare → show exact changes/risks → explicit operation-bound confirmation → execute → audit.
- No employee portal, manager/supervisor accounts, mobile attendance, GPS, inventory, or purchasing module.

## Hotel rules

- Employee codes start at MY-0001 and are never reused. Rehire retains the same employee code and creates a new service period.
- Attendance is entered manually or imported from fingerprint Excel; discrepancies are reviewed before approval.
- HR manually uploads the security roster as an Excel file, specifying each guard's shift start time or OFF for each day; the system does not generate or assign shifts. The shift model is determined by the number of active security guards registered in the system, regardless of OFF entries: exactly two active guards use 12-hour shifts (08:00–20:00 and 20:00–08:00), while three or more use 8-hour shifts (08:00–16:00, 16:00–00:00, and 00:00–08:00). OFF does not change the shift model. The system validates the uploaded roster and presents each day for manual HR approval before saving; after approval, the roster is checked against fingerprint clock-in and clock-out records. Never fewer than two guards assigned per day, no gaps, and no overlap. Overnight shifts belong to their start date.
- Regular advances: request window 15–22 and maximum 25% of eligible basic salary for the accounting month. Installment advances are separate, 1–36 installments.
- Payroll is normally paid on day 5 of the following month. Hourly rate = basic salary ÷ (daily hours × 30). Absence uses actual calendar days; partial first month is prorated by actual calendar days.
- Overtime requires HR approval. Taxes and insurance are manually entered/approved; no guessed rates.
- Bonuses and sanctions are manually entered; no automatically generated amounts.

## Architecture

- Python 3.11+, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16.
- Server-rendered admin UI with local static assets.
- Ollama-compatible local model on loopback only; no employee or payroll information is sent to external AI services.
- Single-use, expiring, actor-bound, plan-hash-bound confirmation tokens and audit history.
- Windows PowerShell setup/start and local backup/restore scripts.

## Development status

**Project control:** [`docs/MASTER_PROJECT_NOTE_V2_2.md`](docs/MASTER_PROJECT_NOTE_V2_2.md) is the single source of truth for instructions, phases, decisions, completed work, test evidence, blockers, and the next task. Read it before every task and update it after every task. Do not use chat history as the project ledger.

**Verified CI baseline:** Commit `83d1079d2edbdf4639a0c5bfcd9f42d731e9a752` passed two GitHub Actions runs: [44886369474](https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2/actions/runs/44886369474) and [44886430837](https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2/actions/runs/44886430837). Linux + PostgreSQL 16, Windows / Python 3.11, and source ZIP packaging completed successfully. The source artifact (`14700078216`) was downloaded and inspected for archive integrity, checksum, and sensitive/local files.

**Release status:** Not production-ready. CI and source-package checks do not prove installation or end-to-end operation on the hotel's Windows 10 computer. Local installation, synthetic HR-to-payroll acceptance, and backup/restore testing remain pending. Use synthetic data only until all V2 acceptance checks pass. See:
- `docs/PROJECT_SPEC.md`
- `docs/AI_ASSISTANT_ARCHITECTURE.md`
- `docs/ACCEPTANCE_CHECKLIST_AR.md`
- `docs/WINDOWS_INSTALL.md`

## Local development

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
# Configure a local PostgreSQL DATABASE_URL and a unique SESSION_SECRET.
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Do not commit `.env`, credentials, database dumps, or real employee data.
