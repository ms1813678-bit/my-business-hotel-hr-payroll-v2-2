"""Desktop launcher: refusal, port handling, clean start/stop. No real server is started."""
import socket
import threading

from app import launcher as lc
from tests.test_launch_guard import make_root


def _run(tmp_path, **kw):
    root = make_root(tmp_path)
    msgs, opened, served = [], [], []
    args = dict(install_root=root, cwd=root, environ={}, message=msgs.append,
                open_browser=opened.append, port_busy=lambda: False,
                app_answers=lambda: True, serve=lambda: served.append(1))
    args.update(kw)
    return lc.run(**args), msgs, opened, served


def test_wrong_folder_refuses_and_starts_nothing(tmp_path):
    root = make_root(tmp_path)
    msgs, served = [], []
    code = lc.run(install_root=root, cwd=tmp_path, environ={}, message=msgs.append,
                  serve=lambda: served.append(1), port_busy=lambda: False)
    assert code == 2 and served == [] and len(msgs) == 1 and "فولدر غلط" in msgs[0]


def test_starts_serves_and_opens_browser(tmp_path):
    code, msgs, opened, served = _run(tmp_path)
    assert code == 0 and served == [1] and msgs == []
    for _ in range(50):
        if opened:
            break
        threading.Event().wait(0.1)
    assert opened == [lc.URL]


def test_port_busy_by_other_program_refuses(tmp_path):
    code, msgs, opened, served = _run(tmp_path, port_busy=lambda: True, app_answers=lambda: False)
    assert code == 2 and served == [] and opened == [] and msgs == [lc.MSG_PORT_BUSY]


def test_already_running_only_opens_browser(tmp_path):
    code, msgs, opened, served = _run(tmp_path, port_busy=lambda: True, app_answers=lambda: True)
    assert code == 0 and served == [] and opened == [lc.URL] and msgs == []


def test_server_crash_shows_message(tmp_path):
    def boom():
        raise RuntimeError("x")
    code, msgs, _, _ = _run(tmp_path, serve=boom)
    assert code == 3 and msgs == [lc.MSG_START_FAILED]


def test_binds_loopback_only():
    assert lc.HOST == "127.0.0.1" and lc.URL.startswith("http://127.0.0.1:")


def test_port_in_use_detects_listener():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); s.listen(1)
    try:
        assert lc.port_in_use("127.0.0.1", s.getsockname()[1]) is True
    finally:
        s.close()
    assert lc.port_in_use("127.0.0.1", 1) is False


def test_uvicorn_config_builds_with_no_console_streams(monkeypatch):
    """pythonw has stdout/stderr = None; the default log formatter would raise."""
    import sys
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    cfg = lc.build_config()
    cfg.load()  # builds the app and loggers like server startup does
    assert cfg.log_config is None


def test_bind_failure_systemexit_shows_message(tmp_path):
    def bind_fail():
        raise SystemExit(1)
    code, msgs, _, _ = _run(tmp_path, serve=bind_fail)
    assert code == 3 and msgs == [lc.MSG_START_FAILED]


def test_clean_systemexit_is_ok(tmp_path):
    def stop():
        raise SystemExit(0)
    code, msgs, _, _ = _run(tmp_path, serve=stop)
    assert code == 0 and msgs == []


def test_not_ready_in_time_shows_arabic_message():
    msgs, opened = [], []
    lc._open_when_ready(opened.append, lambda: False, deadline_s=0.2, message=msgs.append, poll_s=0.05)
    assert opened == [] and msgs == [lc.MSG_NOT_READY]


def test_setup_and_installer_point_to_launcher_and_stop():
    from pathlib import Path
    root = Path(lc.__file__).resolve().parent.parent
    setup = (root / "scripts/windows/setup.ps1").read_text(encoding="utf-8")
    iss = (root / "installer/MYBusinessHotelHR.iss").read_text(encoding="utf-8")
    assert "app.launcher" in setup and "start.ps1" not in setup and "stop.ps1" in setup
    assert "app.launcher" in iss and "stop.ps1" in iss and "autodesktop" not in iss
    assert "launcher.py" not in (root / "scripts/windows/stop.ps1").read_text(encoding="utf-8")


def test_log_is_rotating_file_handler(tmp_path):
    import logging
    import logging.handlers
    root = make_root(tmp_path)
    before = list(logging.getLogger().handlers)
    lc.setup_logging(root)
    added = [h for h in logging.getLogger().handlers if h not in before]
    try:
        assert len(added) == 1 and isinstance(added[0], logging.handlers.RotatingFileHandler)
        assert (root / "var").is_dir()
    finally:
        for h in added:
            logging.getLogger().removeHandler(h)
            h.close()
