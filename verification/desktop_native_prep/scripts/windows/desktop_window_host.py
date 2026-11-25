"""Isolated desktop entry for a direct pythonw shortcut, no PowerShell bypass."""
import sys
from pathlib import Path

# This installation root must already be administrator-controlled.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.desktop_window import main  # noqa: E402

if __name__ == '__main__':
    main()
