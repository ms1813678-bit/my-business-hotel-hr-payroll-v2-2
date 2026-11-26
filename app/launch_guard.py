"""Start-from-the-wrong-folder guard. Standalone: nothing imports or calls it yet (no wiring).

Why: the settings loader reads ``.env`` relative to the current working directory
(app/config.py ``env_file=".env"``). Started from another folder, the program would silently read
a different ``.env`` (possibly another database) or none. This module only decides, it never
starts, stops, creates or changes anything, and it never reads the contents of ``.env``.

``check_launch_context`` is pure over its arguments (paths and a mapping for the environment),
so it can be tested without touching the real folder. A refusal carries a stable ``code`` and a
plain Arabic ``message_ar`` for the person at the laptop.
"""
from __future__ import annotations

import ntpath
import os
from dataclasses import dataclass
from pathlib import Path

# Files that mark the real install root (all must exist as regular files / folders).
ROOT_MARKERS = ("pyproject.toml", "alembic.ini", os.path.join("app", "__init__.py"))

CODE_OK = "ok"
CODE_NOT_ROOT = "install_root_invalid"
CODE_WRONG_CWD = "wrong_working_folder"
CODE_ENV_MISSING = "env_missing"
CODE_ENV_NOT_FILE = "env_not_a_file"

_MESSAGES_AR = {
    CODE_NOT_ROOT: "تعذّر التعرف على مجلد البرنامج. لم يبدأ التشغيل. اتصل بمدير النظام.",
    CODE_WRONG_CWD: "البرنامج اتفتح من فولدر غلط. لم يبدأ التشغيل حتى لا يقرأ إعدادات أو قاعدة بيانات غير مقصودة. افتحه من أيقونة البرنامج أو من فولدر البرنامج نفسه.",
    CODE_ENV_MISSING: "ملف الإعدادات غير موجود في فولدر البرنامج. لم يبدأ التشغيل. اتصل بمدير النظام.",
    CODE_ENV_NOT_FILE: "مكان ملف الإعدادات في فولدر البرنامج ليس ملفًا. لم يبدأ التشغيل. اتصل بمدير النظام.",
}


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    code: str
    message_ar: str = ""


def default_install_root() -> Path:
    """The folder that holds the ``app`` package this module belongs to."""
    return Path(__file__).resolve().parent.parent


def _strip_extended_prefix(path: str) -> str:
    """Drop the Windows extended-length prefix so ``\\\\?\\C:\\x`` compares equal to ``C:\\x``."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\") or path.startswith("\\\\.\\"):
        return path[4:]
    return path


def _same_folder(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    """Equal after resolving links and relative parts; case-insensitive on Windows drives."""
    ra, rb = os.path.realpath(os.fspath(a)), os.path.realpath(os.fspath(b))
    if os.name == "nt" or ntpath.splitdrive(ra)[0]:
        ra, rb = _strip_extended_prefix(ra), _strip_extended_prefix(rb)
        ra, rb = ntpath.normcase(ra), ntpath.normcase(rb)
    return ra == rb


def _refuse(code: str) -> GuardResult:
    return GuardResult(False, code, _MESSAGES_AR[code])


def check_launch_context(install_root=None, cwd=None, environ=None, *, require_env_file: bool = True) -> GuardResult:
    """Decide whether the program may start from ``cwd``.

    Rules, in order (first failure wins):
    1. ``install_root`` must contain the root markers.
    2. ``cwd`` must be the install root (otherwise ``.env`` would resolve elsewhere). An unreadable,
       empty or non-path cwd is refused the same way.
    3. ``<root>/.env`` must exist as a regular file, unless ``require_env_file`` is false or the
       environment already provides DATABASE_URL (only its presence is checked, never its value).
    """
    root = Path(install_root) if install_root is not None else default_install_root()
    try:
        here = os.getcwd() if cwd is None else cwd
    except (OSError, ValueError):  # the current folder was deleted or is unreadable
        return _refuse(CODE_WRONG_CWD)
    env = os.environ if environ is None else environ

    for marker in ROOT_MARKERS:
        if not (root / marker).is_file():
            return _refuse(CODE_NOT_ROOT)
    try:
        if not isinstance(here, (str, os.PathLike)) or not os.fspath(here).strip():
            return _refuse(CODE_WRONG_CWD)
        if not _same_folder(here, root):
            return _refuse(CODE_WRONG_CWD)
    except (OSError, ValueError):  # for example a NUL byte in the path
        return _refuse(CODE_WRONG_CWD)
    env_file = root / ".env"
    if env_file.exists() and not env_file.is_file():
        return _refuse(CODE_ENV_NOT_FILE)
    if require_env_file and not env_file.is_file() and not env.get("DATABASE_URL"):
        return _refuse(CODE_ENV_MISSING)
    return GuardResult(True, CODE_OK)
