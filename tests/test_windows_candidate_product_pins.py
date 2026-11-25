import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PINS = ROOT / "verification/windows_integrated_candidate/PRODUCT-SHA256.json"


def test_candidate_product_pins_match_committed_files():
    source = json.loads((PINS.parent / 'SOURCE-SHA256.json').read_text())
    pins = {**source, **json.loads(PINS.read_text())}
    assert pins
    for rel, pin in pins.items():
        actual = hashlib.sha256((ROOT / rel).read_bytes()).hexdigest()
        assert actual == pin, rel


def test_pinned_files_are_protected_from_line_ending_conversion():
    # Windows checkout converts text files to CRLF, changing the pinned bytes.
    rules = (ROOT / '.gitattributes').read_text().splitlines()
    pins = {**json.loads((PINS.parent / 'SOURCE-SHA256.json').read_text()), **json.loads(PINS.read_text())}
    patterns = [line.split()[0] for line in rules if line.strip() and not line.startswith('#') and line.split()[1:] == ['-text']]
    from fnmatch import fnmatchcase
    for rel in pins:
        assert any(fnmatchcase(rel, pattern.replace('**', '*')) for pattern in patterns), rel
        assert b'\r\n' not in (ROOT / rel).read_bytes(), rel
