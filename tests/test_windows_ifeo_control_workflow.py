from pathlib import Path
import re

W = Path(".github/workflows")
BASE = (W / "windows-integrated-candidate.yml").read_text()
CTRL = (W / "windows-integrated-candidate-ifeo-off.yml").read_text()


def _norm(text):
    text = text.replace("windows-integrated-candidate-ifeo-off.yml", "X")
    text = text.replace("windows-integrated-candidate.yml", "X")
    text = re.sub(r"^name:.*\n", "", text, flags=re.M)
    text = re.sub(r"^# .*\n", "", text, flags=re.M)
    text = text.replace("            & {\n", "            Invoke-WithLoaderSnaps {\n")
    return text.replace("CONTROL ifeo_loader_snaps=OFF. ", "")


def test_control_never_invokes_loader_snaps_or_touches_ifeo():
    assert "Invoke-WithLoaderSnaps" not in CTRL
    assert "Image File Execution Options" not in CTRL
    assert "GlobalFlag" not in CTRL


def test_control_differs_from_candidate_only_by_the_wrapper():
    assert _norm(CTRL) == _norm(BASE)


def test_control_keeps_strict_gates_and_its_own_trigger():
    assert "- .github/workflows/windows-integrated-candidate-ifeo-off.yml" in CTRL
    assert "NOT_ESTABLISHED" in CTRL
    assert "continue-on-error" not in CTRL
    assert "permissions:\n  contents: read" in CTRL


def test_baseline_still_uses_loader_snaps():
    assert BASE.count("Invoke-WithLoaderSnaps {") == 1
