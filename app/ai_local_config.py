"""Where the local assistant's model name and server address come from.

The settings are read once at import (restart the app after editing .env). Precedence, highest first:
process environment, then the ``.env`` file next to the app, then the default.
``LOCAL_AI_MODEL`` is the setting name; ``OLLAMA_MODEL`` is accepted as an
alias because the user guide has always documented that name. The address is
only ever used after the loopback check in ``ai_assistant``; reading it from
``.env`` does not widen what the assistant may connect to.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Mapping

# Single place that names the default. Licence note: qwen2.5:3b is under the Qwen Research
# licence (non-commercial grant); an Apache-2.0 tag must be verified before this changes.
DEFAULT_MODEL = "qwen2.5:3b"
DEFAULT_BASE_URL = "http://127.0.0.1:11434"
MODEL_NAMES = ("LOCAL_AI_MODEL", "OLLAMA_MODEL")
BASE_URL_NAMES = ("LOCAL_AI_BASE_URL",)

# Ollama tags look like "qwen2.5:3b", "library/name:tag" or "name@sha256:..." -
# letters, digits and . _ : / @ - only, no spaces or control characters.
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}$")

_log = logging.getLogger(__name__)

# The Windows launcher (scripts/windows/start.ps1) and the Start-menu shortcut both
# set the working directory to the install folder, where setup.ps1 writes .env, so
# ".env" in the working directory is what Settings reads. Anchoring to the folder
# that holds the app package gives the same file and also works when the app is
# started from another directory.
DEFAULT_DOTENV = str(Path(__file__).resolve().parent.parent / ".env")


def _dotenv(path: str) -> Mapping[str, str | None]:
    try:
        from dotenv import dotenv_values

        if not Path(path).exists():
            return {}
        with open(path, encoding='utf-8-sig') as stream:
            return dotenv_values(stream=stream)
    except ImportError:
        _log.warning('Local AI configuration reader is unavailable; using environment/default values.')
        return {}
    except (OSError, UnicodeError):
        _log.warning('Local AI configuration file could not be read; using environment/default values.')
        return {}


def _first_value(names: tuple[str, ...], env: Mapping[str, str], file_values: Mapping[str, str | None]) -> tuple[str, str] | None:
    for source in (env, file_values):
        for name in names:
            value = source.get(name)
            if value is not None and str(value).strip():
                return name, str(value).strip()
    return None


def resolve_model(env: Mapping[str, str] | None = None, dotenv_path: str = DEFAULT_DOTENV) -> str:
    found = _first_value(MODEL_NAMES, os.environ if env is None else env, _dotenv(dotenv_path))
    if found is None:
        return DEFAULT_MODEL
    name, value = found
    if not _MODEL_RE.fullmatch(value):
        _log.warning("%s is not a valid model name; using the default %s.", name, DEFAULT_MODEL)
        return DEFAULT_MODEL
    return value


def resolve_base_url(env: Mapping[str, str] | None = None, dotenv_path: str = DEFAULT_DOTENV) -> str:
    found = _first_value(BASE_URL_NAMES, os.environ if env is None else env, _dotenv(dotenv_path))
    return (found[1] if found else DEFAULT_BASE_URL).rstrip("/")
