# Requirements bootstrap. Reviewed statically only. Windows UNRAN. No approval to run.

Windows is Windows 10 Pro per the owner (build and bitness unknown). Static review only (several rounds by separate reviewers); the author did not review it as the independent reviewer. Nothing here was run on Windows, nothing is approved to run, and nothing is ready.

## 1. Files
- `check_laptop.bat`: the only thing Mo would double-click later.
- `detect_requirements.ps1`: gathers read-only facts and pipes them as JSON to the classifier.
- `requirements_detect.py`: classifier (Linux-tested), `requirements_manifest.json`: rules.
- `test_requirements_detect.py`: current count in the log; RAN on Linux.

## 2. What the run executes
- Python only, and only a `python.exe` that is on the path allowlist AND whose registry entry (PythonCore) says Version 3.11, in isolated mode (`-I -B`), from the Windows folder as working directory. No file is run from the current folder, no `where.exe`, no `--version` probing, no `py -0p`.
- The allowlist = under Program Files, Program Files (x86), the user's `AppData\Local\Programs` (user-writable), or `<SystemDrive>\Python<n>\`, with the leaf exactly `python.exe`. It is a path allowlist, NOT a trust decision about the file. The Store alias and any WindowsApps path are excluded: a real Store Python reads as missing.
- Plain statement: a Python registered under the user's profile (HKCU) is a file the user chose, and running it executes that file.
- Fixed-folder finds have no registry version, so they are listed only and read UNKNOWN. A PythonCore key without a Version value does the same (a real 3.11 then reads UNKNOWN; this fails safe).
- The classifier is run by one of those probed interpreters (it ran, final 3.x release), never another python.exe. If none was probed, or the run fails, a plain summary prints instead. `requirements_detect.py` beside the script runs UNHASHED, so it is part of what must be reviewed.
- NO database binary is run. No connection, no data folder read, no port opened, no network use, no file written, no setting changed.

## 3. The wrapper flag
`-NoProfile -ExecutionPolicy Bypass -File "%~dp0detect_requirements.ps1"`: process scope only, not saved, no `-Command`. Bypass is not a security control. It does not override a Group Policy MachinePolicy setting, so policy can still block the script; then nothing runs and Mo should simply report that it did not run. It causes no UAC prompt. SmartScreen may show "unknown publisher" for the .bat. Nothing is installed or changed either way.

If SmartScreen, AppLocker, Group Policy or antivirus blocks the .bat or the script, Mo reports the exact message and stops. This document gives no way around any block; it waits for a proper owner decision.

## 4. Results and approvals
Statuses: compatible (a finding), incompatible, unknown, missing. Python 3.11.4 or 3.11.9 meeting the rule is a finding, never an approval; the approval field is empty today. 3.11.9rc1 and other pre-releases are not compatible. 3.12-only is incompatible. Wheels are compatible only with 44 sha lines, 44 files, none unlisted, none missing, no mismatch, all in the lock file; the hash of wheels.sha256 is printed. The source gate says "present, not verified"; the tree is never called verified, an archive hash is only the archive's.

Database: matching is on service or program NAME only (not publisher); client drivers and tools (ODBC, OLE DB, JDBC, connector, client, Management Studio) are listed separately and not counted. Positively PostgreSQL only by exact leaf `pg_ctl.exe` or `postgres.exe` plus file product name containing PostgreSQL plus a parseable version, or an Uninstall entry named `PostgreSQL <n>` with a matching version. 16.x is a finding only when every database-like entry is identified; one PostgreSQL 16 entry beside any unidentified entry (MySQL, SQL Server and so on) reads `unknown`. A program entry without a seen service says "service not seen". Anything else found is `unknown`. `missing` only when collection worked and found nothing, and even then detection is by name patterns only (an unlisted product would not appear); because the owner reports a database IS installed, the shipped manifest flag makes an empty result `unknown`. A PostgreSQL program entry with no service seen is `unknown`, not compatible. The classifier run counts as successful only if it printed its COPYABLE SUMMARY (a Python crash is not mistaken for a result). Program and service names are NOT redacted (also printed by the .bat). Presence never means the program belongs to this project or may be used. The plan never proposes a second database download or starting a service while any database-like entry exists: owner decision only.

## 5. Hashes (status)
| Item | Status |
|---|---|
| Python 3.11.9 installer | python.org publishes MD5 only (e8dcd502e34932eebcaf1be056d5cbcd). BLOCKER: no official SHA-256 |
| PostgreSQL (EDB 16.16-2) | sha256 dd526f2d...c58e128 from a same-host sidecar file. Unverified, not independent |
| GitHub issue comment (EnterpriseDB/edb-installers #816) | author unverified; a lead only; nothing relies on it |

Python 3.11 has no later Windows installer than 3.11.9 (https://www.python.org/downloads/release/python-31118/).

## 6. Intended layout (proposal)
`C:\HotelHR\{bootstrap, option_b, app, wheels, wheels.sha256, archive\}`

## 7. Later install design: PROPOSAL only, nothing implemented or approved
Needs: pinned versions and hashes; re-detect immediately before acting; install only what is missing; PostgreSQL decisions by the owner; rollback; dry-run mode; a UAC prompt where required. Any password goes through a dedicated secrets channel, never chat or the command line. "Signature plus local hash" verification is a proposal for review. Python download is considered only when Python is `missing`.

## 8. Static verification vs missing Windows test
- RAN (Linux): the tests in test_requirements_detect.py, run on the extracted six-file delivery (count in the changelog message and the attached log). Earlier counts in earlier reports are historical. Launcher tests and the app subset are not part of this delivery.
- UNRAN: detect_requirements.ps1, check_laptop.bat (no PowerShell or Windows here). Static text checks are not execution.
- Unverified on a real machine: PEP 514 value names (Version, SysVersion, SysArchitecture); VersionInfo fields on pg_ctl.exe/postgres.exe; Uninstall key shapes; the ps1 JSON field names matching the classifier (checked by fixtures only, two separate codebases).
- Windows verification is a blocker for any qualification. Nothing is ready.

## 9. Does not solve
The standalone window (Option B stays console plus browser tab), offline 3-PC sync (no transport), and the parser fail-closed gate on Windows (stays refused).

## 10. Fixed in this revision
Database detection no longer assumes EDB/pg_ctl; service command lines reduced to exe path; exact leaf match; strict final-version parsing; 3.12 incompatible, not missing; Store-only missing; `count` renamed `file_count`; scalar-vs-list coercion; UTF-8/UTF-16 reader; redaction (path-based: \Users\<name>, and the account or computer name only as a whole path segment; names inside free text such as program names are not redacted, and error text is never printed, only fixed words); short copyable summary and no capture file; no where.exe or cwd execution; py -0p, Smart App Control, citool removed; security check split out.

## 11. Revision 4 fixes
Classifier runner limited to probed candidates, exit code checked, unhashed .py stated; fixed-folder finds documented as listed-only; unidentified DB entry forces unknown; DB name pattern narrowed and drivers split out; redaction by path segment; allowlist wording; .bat message no longer claims success; security file moved out of Mo's folder; errs precedence bug fixed.

## 12. Revision 5 fixes
Runner success judged by the summary text, not exit code; empty DB result unknown while owner reports one installed, with name-pattern limit stated; PostgreSQL program entry without service is unknown; non-redaction of names printed by the .bat and the summary header; service-plus-program wording is 'probably one install seen twice (inferred)'. PARSE CHECK: no pwsh or other PowerShell parser exists here, so detect_requirements.ps1 and check_laptop.bat were NEVER parsed or executed; syntax errors are possible.

## 13. Revision 6
- DB name pattern now uses word boundaries (redis, db2, maria/mariadb, ingres, sql server, firebird, mongodb and others), so "Microsoft Visual C++ Redistributable" no longer counts as a database. Fixtures use realistic Uninstall names: VC++ 2015-2022 Redistributable (x64), Microsoft Edge, Python 3.11.9, OLE DB Driver for SQL Server, PostgreSQL 16. The pattern is read out of the ps1 text and tested with Python re (an approximation of .NET regex; unverified on Windows).
- Counts clarified in section 8.
- No SmartScreen/AppLocker workaround appears anywhere (searched); the section 3 text says to report and stop.

## 14. OPTION, not implemented, not default: sharing results
A possible later improvement for review only: print a short summary with errors and only scrubbed fields, designed to be copied by hand into WhatsApp. A clipboard step (for example Set-Clipboard) is possible but would CHANGE the clipboard, so it must be stated on screen, must never be described as "writes nothing", and must never copy program, service or other private names without scrubbing them first. Nothing here does it today. It needs an owner decision.


## 15. Revision 8 (delivery and docs correction, no functional change)
This delivery is exactly six files: README_BOOTSTRAP.md, check_laptop.bat, detect_requirements.ps1, requirements_detect.py, requirements_manifest.json, test_requirements_detect.py. The optional security script and every reference to it were removed (README, tests, checklist). Two tests that depended on files outside the six were removed: the optional-file test and the check that the lock file sits in a sibling option_b folder. Headers no longer say any revision number or "NOT ready"; they state the true status. The .ps1, .bat, classifier and manifest are byte-identical to the previous revision.
