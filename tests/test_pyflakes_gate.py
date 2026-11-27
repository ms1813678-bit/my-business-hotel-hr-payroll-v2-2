"""Unused imports, redefinitions and repeated dict keys fail CI (Ruff F401, F811, F601 over app and scripts)."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_no_unused_imports_redefinitions_or_repeated_dict_keys():
    try:
        import ruff  # noqa: F401
    except ImportError:
        if shutil.which("ruff") is None:
            pytest.skip("ruff is a dev dependency and is not installed here")
    command = [sys.executable, "-m", "ruff", "check", "--select", "F401,F811,F601", "--no-cache", "app", "scripts"]
    done = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
