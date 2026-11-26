"""Desktop launcher: the one entry point behind the desktop icon.

Run as ``pythonw -m app.launcher`` with the install folder as working directory (the shortcut sets
it). It checks the launch folder with ``launch_guard``, refuses with a plain Arabic message if it
is wrong, starts the app on 127.0.0.1 only, opens the browser once the server answers, and stops
cleanly when the server stops. It never creates, deletes or changes data, ``.env`` or the database.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import urllib.request
import webbrowser

from app import launch_guard

HOST = "127.0.0.1"
PORT = 8000
URL = f"http://{HOST}:{PORT}"
TITLE = "M.Y. Business Hotel HR"

MSG_PORT_BUSY = "المنفذ 8000 مشغول ببرنامج آخر. لم يبدأ التشغيل. اتصل بمدير النظام."
MSG_START_FAILED = "تعذّر تشغيل البرنامج. اتصل بمدير النظام."
MSG_NOT_READY = "البرنامج لم يجهز بعد. تأكد أن قاعدة البيانات تعمل ثم افتح البرنامج مرة أخرى، أو اتصل بمدير النظام."
LOG_NAME = os.path.join("var", "launcher.log")


def show_message(text: str) -> None:
    """Show ``text`` to the person at the laptop (pythonw has no console)."""
    if os.name == "nt":
        try:
            import ctypes

            # MB_OK | MB_ICONERROR | MB_RIGHT | MB_RTLREADING
            ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10 | 0x80000 | 0x100000)
            return
        except Exception:  # fall through to stderr
            pass
    print(text, file=sys.stderr)


def port_in_use(host: str = HOST, port: int = PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def our_app_answers(url: str = URL, timeout: float = 2.0) -> bool:
    """True if the program is already running there (its /health answers with status ok)."""
    try:
        with urllib.request.urlopen(url + "/health", timeout=timeout) as r:
            return r.status == 200 and b"ok" in r.read(2048).lower()
    except Exception:
        return False


def _open_when_ready(open_browser, is_up, deadline_s: float = 60.0, message=None, poll_s: float = 1.0) -> None:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if is_up():
            open_browser(URL)
            return
        time.sleep(poll_s)
    if message is not None:
        message(MSG_NOT_READY)


def setup_logging(install_root) -> None:
    """Log to a file under the install folder. Under pythonw there is no console (streams are None)."""
    try:
        path = os.path.join(os.fspath(install_root), LOG_NAME)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        root_logger = logging.getLogger()
        root_logger.addHandler(handler)
        root_logger.setLevel(logging.INFO)
    except OSError:
        logging.disable(logging.CRITICAL)  # never fail to start because a log file is unwritable


def build_config():
    """uvicorn config that works with no console: log_config=None (the default formatter needs a stream)."""
    import uvicorn

    return uvicorn.Config("app.main:app", host=HOST, port=PORT, log_config=None)


def run(*, install_root=None, cwd=None, environ=None, serve=None, open_browser=webbrowser.open,
        message=show_message, port_busy=port_in_use, app_answers=our_app_answers) -> int:
    """Return an exit code: 0 started and stopped normally, 2 refused, 3 failed to start."""
    result = launch_guard.check_launch_context(install_root, cwd, environ)
    if not result.ok:
        message(result.message_ar)
        return 2
    if port_busy():
        if app_answers():  # already running: just bring the browser up, start nothing
            open_browser(URL)
            return 0
        message(MSG_PORT_BUSY)
        return 2
    if serve is None:
        def serve():
            import uvicorn

            uvicorn.Server(build_config()).run()
        setup_logging(launch_guard.default_install_root() if install_root is None else install_root)
    threading.Thread(target=_open_when_ready, args=(open_browser, app_answers),
                     kwargs={"message": message}, daemon=True).start()
    try:
        serve()
    except SystemExit as e:
        if e.code in (0, None):
            return 0
        message(MSG_START_FAILED)
        return 3
    except Exception:
        message(MSG_START_FAILED)
        return 3
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
