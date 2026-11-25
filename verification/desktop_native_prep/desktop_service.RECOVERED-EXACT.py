"""Windows SCM adapter, intentionally separate from the desktop window."""
import sys
import threading
import time
from pathlib import Path

import servicemanager
import win32service
import win32serviceutil

from app.desktop_config import DesktopConfig
from app.desktop_diagnostics import failure_structure
from app.desktop_server import config_path, create_server


class HOnYService(win32serviceutil.ServiceFramework):
    _exe_name_ = sys.executable
    _exe_args_ = '-I "' + str(Path(__file__).resolve().parents[1] / "scripts" / "windows" / "service_host.py") + '"'
    _svc_name_ = "HOnYBackend"
    _svc_display_name_ = "HOnY Hotel HTTPS Backend"
    _svc_description_ = "Local hotel HTTPS backend. Closing the HOnY window does not stop this service."

    def __init__(self, args):
        super().__init__(args)
        self.stop_requested = threading.Event()
        self.server = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=5000)
        self.stop_requested.set()
        if self.server is not None:
            self.server.should_exit = True

    def SvcShutdown(self):
        self.SvcStop()

    def SvcDoRun(self):
        try:
            config = DesktopConfig.load(config_path())
            self.server = create_server(config)
            if self.stop_requested.is_set():
                return
            failures = []
            def serve():
                try:
                    self.server.run()
                except BaseException as exc:  # noqa: BLE001 - propagate worker failure to SCM
                    failures.append(failure_structure(exc))
                    servicemanager.LogErrorMsg("HOnY backend worker failure " + failures[-1])
            worker = threading.Thread(target=serve, name="hony-https", daemon=True)
            worker.start()
            deadline = None
            while worker.is_alive():
                worker.join(1)
                if self.stop_requested.is_set():
                    self.server.should_exit = True
                    if deadline is None:
                        deadline = time.monotonic() + 30
                    self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING, waitHint=5000)
                    if time.monotonic() >= deadline:
                        self.server.force_exit = True
                        # An intentional stop must not become an SCM failure
                        # that triggers configured restart actions. The hosted
                        # process owns this daemon worker and exits after the
                        # dispatcher returns. Actual Windows process teardown
                        # and open-handle behavior still need acceptance tests.
                        servicemanager.LogErrorMsg("HOnY intentional stop exceeded grace; worker force-exit requested. Verify process teardown.")
                        return
            if self.stop_requested.is_set():
                # Worker exceptions during an explicit stop are already logged;
                # do not turn an operator stop into a configured failure restart.
                return
            raise RuntimeError("HTTPS backend exited without a service stop request.")
        except Exception as exc:
            # Do not put configuration values, DB URLs, or TLS paths in events.
            servicemanager.LogErrorMsg("HOnY backend failed " + failure_structure(exc))
            raise


if __name__ == "__main__":
    win32serviceutil.HandleCommandLine(HOnYService)
