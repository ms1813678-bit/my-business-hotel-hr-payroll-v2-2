"""Spreadsheet export safety: stored text must never become a live formula.

openpyxl turns any string that starts with "=" into a formula cell. Report
exports write employee names, departments, titles and reasons that users type,
so such a value would run when the file is opened. None of the report exports
write intentional formulas, so every formula-typed cell is stored as text.
The visible value is unchanged; numbers, dates and booleans are not touched.
"""
from __future__ import annotations


def neutralize_formula_cells(workbook) -> int:
    """Store formula-typed cells as text. Returns how many cells were changed."""
    changed = 0
    for sheet in workbook.worksheets:
        for row in sheet.iter_rows():
            for cell in row:
                if cell.data_type == "f":
                    cell.data_type = "s"
                    changed += 1
    return changed
