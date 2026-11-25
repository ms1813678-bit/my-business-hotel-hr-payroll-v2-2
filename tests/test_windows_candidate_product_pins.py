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
