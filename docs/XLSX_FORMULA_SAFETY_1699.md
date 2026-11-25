# XLSX typed-text safety qualification, 25 November 2026

Baseline: published 5a3b62d, with the supplied three-file candidate from cabb5c9 applied unchanged. This fixes export serialization, not report selection, calculations, permissions or input storage.

Both comprehensive HR and custom report writers emit calculated values, not intentional spreadsheet formulas. openpyxl 3.1.5 inferred a leading-equals string as a formula. The final-save helper changes only formula-typed cells to text, preserving their literal values. It runs after translation rewrites on every worksheet. Numbers, dates and booleans are untouched. The existing department workbook already stores text explicitly and is unchanged. A future intentional formula feature must revisit this contract rather than silently adding formulas here.

## Qualification

- 155 passed, zero skipped, two warnings in 32.12 seconds: nine new tests plus reports, report builder, monthly reports, manual report CLI, roles and route security. PostgreSQL runtime was configured, so the earlier worker's SQLite-only route environment failures did not occur.
- Own negative control: disable only the two final-save calls, retain helper and tests. Six language-parametrized export assertions fail and three helper/ordinary-value tests pass in 0.92 seconds. Calls restored. This experiment is not a product mode.
- Own separate actual PostgreSQL 16.2 READ COMMITTED schema migrated through 0067: seeded three synthetic employees and advances, exported HR and custom workbooks in Arabic, English and Chinese. All six have zero formula-typed cells and zero worksheet XML formula elements. Leading-equals arithmetic, HYPERLINK, department and position data remain literal strings; HR salary remains numeric 1000 and hire date remains a date. No schema retained after proof. The report reads do not update the synthetic source records.
- Original worker's nine automated cases use SQLite and include plus, minus, at-sign, tab and equals strings, Arabic and ordinary text. These remain text where openpyxl already encoded them as text. No claim of a CSV import sanitizer.
- Own LibreOffice 7.3.7.2 PDF export of the actual PG-generated English HR/custom workbooks inspected as pixels: arithmetic text displays literally rather than 2, and HYPERLINK text remains text. Existing long custom text clips and HR print layout splits across pages, not changed or represented as fixed here. Pixel proof is not Microsoft Excel or hotel-device acceptance.

No external hyperlink was opened. No real employee records, device, account or installed application were used. No new dependency or database migration. Existing report access checks remain unchanged.

Final exact shared-tree run:155passed0skipped2warnings30.58s with unchanged candidate and selection. Independent static review verified all four file hashes, both baseline import/call-only diffs, final placement and coverage of all XLSX save sites. It did not rerun tests or re-read every row-building line. No further product edit followed qualification.
