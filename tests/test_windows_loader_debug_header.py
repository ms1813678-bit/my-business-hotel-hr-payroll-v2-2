import re
from pathlib import Path

HEADER = Path(__file__).resolve().parents[1] / "verification/windows_integrated_candidate/loader_debug.h"
TEXT = HEADER.read_text()


def test_module_basename_and_summary_are_emitted():
    assert "loader_debug_module name=%s clamped=%d dir=%s" in TEXT
    assert "loader_debug_summary loads=%u last_module=%s" in TEXT
    assert "loader_debug_module(event.u.LoadDll.hFile,s)" in TEXT


def test_no_debug_string_text_or_tokens_are_printed():
    assert "dll_token" not in TEXT.replace("dll_token_", "dll_token")  # block removed
    printed = [line for line in TEXT.splitlines() if "fprintf" in line]
    for line in printed:
        if re.search(r"\bsafe\b", line):
            assert "loader_debug_module name=%s" in line or "keyword_bits" in line


def test_sanitised_charset_and_fixed_vocabulary():
    assert "ch==L'.'||ch==L'_'||ch==L'-'" in TEXT
    words = re.search(r"const char \*words\[\]=\{(.*?)\};", TEXT).group(1)
    assert len(re.findall(r'"[^"]+"', words)) <= 32


def test_acceptance_gates_are_untouched_by_this_header():
    for needle in ("SetSecurityInfo", "AdjustTokenPrivileges", "CreateRestrictedToken", "socket"):
        assert needle not in TEXT
