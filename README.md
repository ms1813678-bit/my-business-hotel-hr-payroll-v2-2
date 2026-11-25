# M.Y. Business Hotel — HR & Payroll System — Version 2.2

**Independent Version 2.2 | M.Y. Business Hotel — Maadi, Cairo**

A local-first HR and payroll application for Windows 10/11, using PostgreSQL on the hotel computer. Version 2 is developed in this repository only. The previous repository is a read-only reference and must not be modified as part of V2 work.

## Current implementation status

Consult and maintain [the current project decision notebook](docs/PROJECT_DECISIONS_NOTEBOOK_AR.md) before project work or policy questions. It records original-decision references, partial amendments, remaining work and historical limits. Historical task lists do not grant permissions or establish current acceptance.

Read [the current source/status index](docs/CURRENT_SOURCE_STATUS.md) before using the older setup, architecture or handoff text below. Development is on `ui-overhaul`; daily overtime has a bounded published implementation; its remaining limits are recorded in the current index. Windows installation and hotel-device acceptance are not complete.

## Product commitments

- Supported roles include HR/System Administrator, finance and the subordinate assistant. Role-specific powers must follow current verified permissions; no employee or department-manager login is implied.
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
- Overtime payroll access includes HR/System Administrator and finance, not the assistant. Published overtime uses basic plus performance only, divided by30 and the selected rate divisor. Daily review, append-only correction and monthly aggregation have a bounded published implementation; see the current status index for its qualification and remaining acceptance limits. Taxes and insurance remain manual, with no guessed rates.
- Bonuses and sanctions are manually entered; no automatically generated amounts.

## Architecture

- Python 3.11+, FastAPI, SQLAlchemy 2, Alembic, PostgreSQL 16.
- Server-rendered admin UI with local static assets.
- Ollama-compatible local model on loopback only; no employee or payroll information is sent to external AI services.
- Single-use, expiring, actor-bound, plan-hash-bound confirmation tokens and audit history.
- Windows PowerShell setup/start and local backup/restore scripts.

## Development status

**Project control:** [`docs/PROJECT_DECISIONS_NOTEBOOK_AR.md`](docs/PROJECT_DECISIONS_NOTEBOOK_AR.md) is the maintained decision and work index. Consult original owner messages for authority and unresolved source gaps. [`docs/MASTER_PROJECT_NOTE_V2_2.md`](docs/MASTER_PROJECT_NOTE_V2_2.md) retains historical detail; it does not override the current notebook or current source evidence.

**Historical CI baseline, not current release acceptance:** Commit `83d1079d2edbdf4639a0c5bfcd9f42d731e9a752` passed two GitHub Actions runs: [44886369474](https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2/actions/runs/44886369474) and [44886430837](https://github.com/ms1813678-bit/my-business-hotel-hr-payroll-v2-2/actions/runs/44886430837). Linux + PostgreSQL 16, Windows / Python 3.11, and source ZIP packaging completed successfully. The source artifact (`14700078216`) was downloaded and inspected for archive integrity, checksum, and sensitive/local files.

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
