"""Structural validation for Egyptian national ID numbers.

Structure only: 14 digits, a century digit of 2 or 3, a real calendar birth
date, and a documented governorate code. The final check digit is NOT
verified (no verified algorithm), and a passing number is not proof the
number was actually issued. Applies to new or changed values only; existing
records are never revalidated, and the field stays optional.
"""
from datetime import date

# Documented governorate registration codes from third-party references
# (not an official government publication): 27 governorates plus 88 for
# births registered outside the republic.
GOVERNORATE_CODES = frozenset({
    "01", "02", "03", "04",
    "11", "12", "13", "14", "15", "16", "17", "18", "19",
    "21", "22", "23", "24", "25", "26", "27", "28", "29",
    "31", "32", "33", "34", "35",
    "88",
})

ERROR_MESSAGE = (
    "الرقم القومي يجب أن يكون 14 رقمًا بتركيب صحيح: "
    "رقم القرن ثم تاريخ ميلاد صحيح ثم كود محافظة معروف."
)


def is_valid_egyptian_national_id(value: str) -> bool:
    if len(value) != 14 or not value.isdigit():
        return False
    century = value[0]
    if century not in ("2", "3"):
        return False
    year = (1900 if century == "2" else 2000) + int(value[1:3])
    try:
        date(year, int(value[3:5]), int(value[5:7]))
    except ValueError:
        return False
    return value[7:9] in GOVERNORATE_CODES
