"""requirements.lock must pin every pyproject dependency exactly, with hashes, from wheels only."""
import re
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def _lock():
    pins, current = {}, None
    for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z0-9_.\-]+(?:\[[^\]]+\])?)==([^\s\\]+)", line)
        if match:
            current = canonicalize_name(match.group(1).split("[")[0])
            pins[current] = {"version": match.group(2), "hashes": 0}
        elif current and "--hash=sha256:" in line:
            pins[current]["hashes"] += len(re.findall(r"--hash=sha256:[0-9a-f]{64}", line))
    return pins


def test_every_pin_has_hashes_and_nothing_is_a_url_or_range():
    text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert "git+" not in text and "http://" not in text and "--index-url" not in text
    pins = _lock()
    assert len(pins) > 30
    assert all(p["hashes"] >= 1 for p in pins.values()), [n for n, p in pins.items() if not p["hashes"]]


def test_every_pyproject_dependency_is_pinned_inside_its_range():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    pins = _lock()
    for dependency in project["dependencies"]:
        requirement = Requirement(dependency)
        name = canonicalize_name(requirement.name)
        assert name in pins, name
        assert requirement.specifier.contains(Version(pins[name]["version"]), prereleases=False), (name, pins[name])


def test_setup_installs_only_from_the_lock_and_installer_ships_it():
    setup = (ROOT / "scripts" / "windows" / "setup.ps1").read_text(encoding="utf-8")
    assert "--require-hashes --no-deps --only-binary=:all:" in setup and "requirements.lock" in setup
    assert "pip install -e ." not in setup and "pip install --upgrade pip" not in setup
    assert "requirements.lock" in (ROOT / "installer" / "MYBusinessHotelHR.iss").read_text(encoding="utf-8")
