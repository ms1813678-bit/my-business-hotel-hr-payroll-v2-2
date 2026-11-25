"""SCM entry point under the same venv interpreter as installation.

Uses the upstream python.exe-hosted service pattern, avoiding implicit
pythonservice.exe interpreter selection. Windows acceptance is still required.
"""
import sys
from pathlib import Path

# SCM starts in System32. Add only the administrator-controlled app root.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import servicemanager  # noqa: E402

from app.desktop_service import HOnYService  # noqa: E402

if __name__ == '__main__':
    servicemanager.Initialize()
    servicemanager.PrepareToHostSingle(HOnYService)
    servicemanager.StartServiceCtrlDispatcher()
