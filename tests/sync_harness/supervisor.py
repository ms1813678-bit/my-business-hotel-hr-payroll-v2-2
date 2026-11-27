"""Start, stop, kill and restart the three instance processes."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import client, provision


class SupervisorError(RuntimeError):
    pass


@dataclass
class InstanceSpec:
    name: str                     # A (main), B, C
    role: str                     # main | device
    device_id: str
    role_name: str
    db_url: str
    port: int = field(default_factory=provision.free_port)
    clock_offset_seconds: int = 0
    fault: str | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@dataclass
class RunningInstance:
    spec: InstanceSpec
    log_path: Path
    process: subprocess.Popen | None = None
    exit_code: int | None = None


class Supervisor:
    def __init__(self, specs: list[InstanceSpec], work_dir: Path, *, engine: str = "auto",
                 repo_root: Path | None = None, python: str | None = None):
        self.work_dir = Path(work_dir)
        self.log_dir = self.work_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.engine = engine
        self.repo_root = Path(repo_root or Path(__file__).resolve().parents[2])
        self.python = python or sys.executable
        self.instances = {spec.name: RunningInstance(spec=spec,
                                                     log_path=self.log_dir / f"{spec.name}.log")
                          for spec in specs}
        self.main_url = self.instances["A"].spec.base_url if "A" in self.instances else ""

    # -- process control

    def _command(self, spec: InstanceSpec) -> list[str]:
        command = [self.python, "-m", "tests.sync_harness.server",
                   "--role", spec.role, "--db-url", spec.db_url, "--port", str(spec.port),
                   "--device-id", spec.device_id, "--role-name", spec.role_name,
                   "--main-url", self.main_url, "--engine", self.engine,
                   "--clock-offset-seconds", str(spec.clock_offset_seconds)]
        if spec.fault:
            command += ["--fault", spec.fault]
        return command

    def start(self, name: str) -> None:
        instance = self.instances[name]
        if instance.process is not None and instance.process.poll() is None:
            return
        env = dict(os.environ)
        env.update({"DATABASE_URL": instance.spec.db_url,
                    "SESSION_SECRET": "sync-harness-only-session-secret-0123456789",
                    "APP_ENV": "test", "PYTHONPATH": str(self.repo_root),
                    "PYTHONUNBUFFERED": "1"})
        log = open(instance.log_path, "ab", buffering=0)
        log.write(f"\n===== start {name} {time.strftime('%H:%M:%S')} =====\n".encode())
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        instance.process = subprocess.Popen(self._command(instance.spec), cwd=str(self.repo_root),
                                            env=env, stdout=log, stderr=subprocess.STDOUT,
                                            creationflags=creationflags)
        instance.exit_code = None
        self.wait_ready(name)

    def wait_ready(self, name: str, *, timeout: float = 60.0) -> None:
        instance = self.instances[name]
        deadline = time.time() + timeout
        while time.time() < deadline:
            process = instance.process
            if process is not None and process.poll() is not None:
                instance.exit_code = process.returncode
                raise SupervisorError(
                    f"{name} exited with {process.returncode} before it was ready\n"
                    f"{self.log_tail(name)}")
            try:
                client.get_json(instance.spec.base_url + "/harness/health", timeout=2.0)
                return
            except Exception:
                time.sleep(0.15)
        raise SupervisorError(f"{name} was not ready within {timeout}s\n{self.log_tail(name)}")

    def stop(self, name: str, *, graceful: bool = True) -> None:
        instance = self.instances[name]
        process = instance.process
        if process is None or process.poll() is not None:
            instance.exit_code = None if process is None else process.returncode
            instance.process = None
            return
        if graceful:
            try:
                client.post_json(instance.spec.base_url + "/harness/shutdown", {}, timeout=5.0)
            except Exception:
                pass
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        else:
            process.kill()
            process.wait(timeout=10)
        instance.exit_code = process.returncode
        instance.process = None

    def kill(self, name: str) -> int | None:
        """Kill without cleanup, the way a power cut does."""
        self.stop(name, graceful=False)
        return self.instances[name].exit_code

    def restart(self, name: str) -> None:
        self.stop(name, graceful=False)
        self.start(name)

    def start_all(self) -> None:
        for name in self.instances:
            self.start(name)

    def stop_all(self) -> None:
        for name in list(self.instances):
            try:
                self.stop(name)
            except Exception:
                pass

    # -- helpers

    def client(self, name: str) -> client.HarnessClient:
        return client.HarnessClient(self.instances[name].spec.base_url)

    def spec(self, name: str) -> InstanceSpec:
        return self.instances[name].spec

    def log_tail(self, name: str, lines: int = 60) -> str:
        path = self.instances[name].log_path
        if not path.exists():
            return f"(no log for {name})"
        text = path.read_text("utf-8", errors="replace").splitlines()
        return "\n".join(text[-lines:])

    def alive(self, name: str) -> bool:
        process = self.instances[name].process
        return process is not None and process.poll() is None

    def wait_exit(self, name: str, *, timeout: float = 30.0) -> int | None:
        """Wait for a process to die on its own (a fault kill) and return its code."""
        instance = self.instances[name]
        process = instance.process
        if process is None:
            return instance.exit_code
        deadline = time.time() + timeout
        while time.time() < deadline:
            code = process.poll()
            if code is not None:
                instance.exit_code = code
                instance.process = None
                return code
            time.sleep(0.05)
        raise SupervisorError(f"{name} did not exit within {timeout}s\n{self.log_tail(name)}")
