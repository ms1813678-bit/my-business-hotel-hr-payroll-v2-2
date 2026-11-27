"""PostgreSQL instances for the sync harness.

Each instance (main, device B, device C) gets its own PostgreSQL server on its
own port, which is what the design note asks for ("its own PostgreSQL database
and port"). Two ways to get one:

* embedded (default): the PostgreSQL binaries that ship inside the ``pgserver``
  wheel (test-only dependency) are driven directly with initdb/pg_ctl, so Linux
  and Windows runners behave the same and nothing needs root or a service.
* external (``--pg-external``): the server named by PGHOST/PGPORT/PGUSER/
  PGPASSWORD, the way the repository's other PostgreSQL tests run in CI.

Schema comes from the real migration chain: ``scripts.initialize_database``
builds one template database per server, and each scenario's databases are
cloned from it with CREATE DATABASE ... TEMPLATE. A clone is a few hundred
milliseconds, so a scenario starts from a migrated, empty database rather than a
create_all approximation the product would never ship.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

IS_WINDOWS = platform.system() == "Windows"


class ProvisionError(RuntimeError):
    pass


STEP_TIMEOUT = float(os.environ.get("SYNC_HARNESS_STEP_TIMEOUT", "120"))


def step(message: str) -> None:
    """Progress line, flushed before the step runs, so a hang names its step."""
    print(f"[{time.strftime('%H:%M:%S')}] step: {message}", flush=True)


def run_tool(args: list[str], *, timeout: float | None = None, label: str = "",
             **kwargs) -> subprocess.CompletedProcess:
    """subprocess.run with stdin closed, output captured and a hard timeout.

    stdin is closed because a child that inherits a console handle can block on
    Windows. On timeout the child is killed and a ProvisionError names it.
    """
    timeout = STEP_TIMEOUT if timeout is None else timeout
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired as exc:
        tail = ""
        for stream in (exc.stdout, exc.stderr):
            if stream:
                tail += (stream if isinstance(stream, str) else stream.decode("utf-8", "replace"))[-400:]
        raise ProvisionError(f"timed out after {timeout:.0f}s: {label or ' '.join(map(str, args))[:200]} {tail}")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def pg_bin_dir(explicit: str | None = None) -> Path:
    """Where initdb/pg_ctl/psql/pg_dump live."""
    for candidate in (explicit, os.environ.get("SYNC_HARNESS_PG_BIN")):
        if candidate:
            path = Path(candidate)
            if (path / _exe("initdb")).exists():
                return path
            raise ProvisionError(f"no initdb in {path}")
    try:
        import pgserver

        bundled = Path(pgserver.__file__).parent / "pginstall" / "bin"
        if (bundled / _exe("initdb")).exists():
            return bundled
    except ImportError:
        pass
    found = shutil.which("initdb")
    if found:
        return Path(found).parent
    raise ProvisionError(
        "no PostgreSQL binaries found: install the test dependency pgserver "
        "(pip install pgserver) or set SYNC_HARNESS_PG_BIN"
    )


def _tail(path: Path, chars: int = 800) -> str:
    try:
        return Path(path).read_text("utf-8", errors="replace")[-chars:]
    except OSError:
        return "(unreadable)"


def _exe(name: str) -> str:
    return name + (".exe" if IS_WINDOWS else "")


class Server:
    """One PostgreSQL server; subclasses own how it starts and stops."""

    name = "server"

    def url(self, database: str) -> str:
        raise NotImplementedError

    def sql(self, database: str, statement: str) -> str:
        raise NotImplementedError

    def create_database(self, database: str, template: str | None = None) -> None:
        clause = f" TEMPLATE {template}" if template else ""
        self.sql("postgres", f'CREATE DATABASE "{database}"{clause}')

    def drop_database(self, database: str) -> None:
        self.sql("postgres", f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')

    def dump(self, database: str, target: Path) -> None:
        raise NotImplementedError

    def restore(self, database: str, source: Path) -> None:
        raise NotImplementedError

    def close(self) -> None:
        pass


@dataclass
class EmbeddedServer(Server):
    datadir: Path
    port: int = field(default_factory=free_port)
    bin_dir: Path = field(default_factory=pg_bin_dir)
    started: bool = False

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"pg:{self.datadir.name}"

    def _run(self, args: list[str], *, quiet: bool = False, **kwargs) -> subprocess.CompletedProcess:
        if not quiet:
            step(f"{self.name} {args[0]} {' '.join(args[1:3])}")
        return run_tool([str(self.bin_dir / _exe(args[0])), *args[1:]], label=args[0], **kwargs)

    def start(self) -> None:
        if self.started:
            return
        self.datadir.parent.mkdir(parents=True, exist_ok=True)
        if not (self.datadir / "PG_VERSION").exists():
            done = self._run(["initdb", "-D", str(self.datadir), "-U", "postgres",
                              "--auth=trust", "--encoding=UTF8", "--locale=C"])
            if done.returncode:
                raise ProvisionError(f"initdb failed: {done.stderr[-800:]}")
        options = f"-p {self.port} -h 127.0.0.1"
        if not IS_WINDOWS:
            # Unix socket paths are limited to about 107 bytes; a deep work directory
            # (CI checkouts are deep) would exceed that, so the socket lives in a short temp dir.
            socket_dir = Path(tempfile.mkdtemp(prefix="syh"))
            self.socket_dir = socket_dir
            options += f" -k {socket_dir}"
        log = self.datadir.parent / (self.datadir.name + ".log")
        # The postmaster outlives pg_ctl. If pg_ctl's stdout/stderr were pipes, the
        # postmaster would inherit them and subprocess would wait for EOF forever
        # (a known Windows hang), so start it with files and no pipes.
        step(f"{self.name} pg_ctl start on port {self.port}")
        ctl_out = self.datadir.parent / (self.datadir.name + ".pg_ctl.txt")
        with open(ctl_out, "wb") as sink:
            try:
                code = subprocess.run(
                    [str(self.bin_dir / _exe("pg_ctl")), "-D", str(self.datadir), "-w",
                     "-t", "60", "-l", str(log), "-o", options, "start"],
                    stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
                    timeout=STEP_TIMEOUT).returncode
            except subprocess.TimeoutExpired:
                raise ProvisionError(f"pg_ctl start timed out on {self.name}: "
                                     f"{_tail(ctl_out)} | server log: {_tail(log)}")
        if code:
            raise ProvisionError(f"pg_ctl start failed ({code}): {_tail(ctl_out)} | "
                                 f"server log: {_tail(log)}")
        self.started = True
        self._wait_ready()

    def _wait_ready(self) -> None:
        deadline = time.time() + 30
        while time.time() < deadline:
            if self._run(["pg_isready", "-h", "127.0.0.1", "-p", str(self.port), "-U", "postgres"], quiet=True, timeout=15).returncode == 0:
                return
            time.sleep(0.1)
        raise ProvisionError(f"{self.name} did not become ready on port {self.port}: "
                             f"{_tail(self.datadir.parent / (self.datadir.name + '.log'))}")

    def url(self, database: str) -> str:
        return f"postgresql+psycopg://postgres@127.0.0.1:{self.port}/{database}"

    def sql(self, database: str, statement: str) -> str:
        done = self._run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", "-h", "127.0.0.1",
                          "-p", str(self.port), "-U", "postgres", "-d", database, "-c", statement])
        if done.returncode:
            raise ProvisionError(f"psql failed on {self.name}/{database}: {done.stderr[-800:]}")
        return done.stdout

    def dump(self, database: str, target: Path) -> None:
        done = self._run(["pg_dump", "--format=custom", "--no-owner", "--no-acl",
                          "-h", "127.0.0.1", "-p", str(self.port), "-U", "postgres",
                          "-d", database, "-f", str(target)])
        if done.returncode:
            raise ProvisionError(f"pg_dump failed: {done.stderr[-800:]}")

    def restore(self, database: str, source: Path) -> None:
        done = self._run(["pg_restore", "--no-owner", "--no-acl", "--clean", "--if-exists",
                          "--single-transaction", "-h", "127.0.0.1", "-p", str(self.port),
                          "-U", "postgres", "-d", database, str(source)])
        if done.returncode:
            raise ProvisionError(f"pg_restore failed: {done.stderr[-800:]}")

    def close(self) -> None:
        if not self.started:
            return
        self._run(["pg_ctl", "-D", str(self.datadir), "-m", "immediate", "stop"])
        self.started = False
        socket_dir = getattr(self, "socket_dir", None)
        if socket_dir is not None:
            shutil.rmtree(socket_dir, ignore_errors=True)


@dataclass
class ExternalServer(Server):
    """A PostgreSQL server the runner already provides (CI service container)."""

    host: str
    port: int
    user: str
    password: str
    bin_dir: Path = field(default_factory=pg_bin_dir)

    @property
    def name(self) -> str:  # type: ignore[override]
        return f"pg:{self.host}:{self.port}"

    def _env(self) -> dict:
        return dict(os.environ, PGPASSWORD=self.password)

    def url(self, database: str) -> str:
        credentials = f"{self.user}:{self.password}" if self.password else self.user
        return f"postgresql+psycopg://{credentials}@{self.host}:{self.port}/{database}"

    def sql(self, database: str, statement: str) -> str:
        done = run_tool([str(self.bin_dir / _exe("psql")), "-X", "-q", "-v", "ON_ERROR_STOP=1",
                               "-h", self.host, "-p", str(self.port), "-U", self.user,
                               "-d", database, "-c", statement],
                              env=self._env())
        if done.returncode:
            raise ProvisionError(f"psql failed on {self.name}/{database}: {done.stderr[-800:]}")
        return done.stdout

    def dump(self, database: str, target: Path) -> None:
        done = run_tool([str(self.bin_dir / _exe("pg_dump")), "--format=custom", "--no-owner",
                               "--no-acl", "-h", self.host, "-p", str(self.port), "-U", self.user,
                               "-d", database, "-f", str(target)],
                              env=self._env())
        if done.returncode:
            raise ProvisionError(f"pg_dump failed: {done.stderr[-800:]}")

    def restore(self, database: str, source: Path) -> None:
        done = run_tool([str(self.bin_dir / _exe("pg_restore")), "--no-owner", "--no-acl",
                               "--clean", "--if-exists", "--single-transaction", "-h", self.host,
                               "-p", str(self.port), "-U", self.user, "-d", database, str(source)],
                              env=self._env())
        if done.returncode:
            raise ProvisionError(f"pg_restore failed: {done.stderr[-800:]}")


INSTANCE_NAMES = ("A", "B", "C")
DATABASES = {"A": "hotel_main", "B": "hotel_device_b", "C": "hotel_device_c"}


class Provisioner:
    """Three servers, one migrated template each, databases cloned per scenario."""

    def __init__(self, work_dir: Path, *, external: bool = False, pg_bin: str | None = None,
                 repo_root: Path | None = None):
        self.work_dir = Path(work_dir)
        self.repo_root = Path(repo_root or Path(__file__).resolve().parents[2])
        self.external = external
        self.pg_bin = pg_bin_dir(pg_bin) if pg_bin or not external else None
        self.servers: dict[str, Server] = {}
        self.template = "hotel_template"
        self._prepared = False

    # -- lifecycle

    def prepare(self) -> None:
        self.work_dir.mkdir(parents=True, exist_ok=True)
        step(f"prepare: binaries in {self.pg_bin}")
        for name in INSTANCE_NAMES:
            if self.external:
                server = ExternalServer(
                    host=os.environ.get("PGHOST", "127.0.0.1"),
                    port=int(os.environ.get("PGPORT", "5432")),
                    user=os.environ.get("PGUSER", "postgres"),
                    password=os.environ.get("PGPASSWORD", ""),
                )
            else:
                server = EmbeddedServer(datadir=self.work_dir / f"cluster_{name}" / "data",
                                        bin_dir=self.pg_bin)
            self.servers[name] = server
        for server in {id(s): s for s in self.servers.values()}.values():
            if isinstance(server, EmbeddedServer):
                server.start()
        self._build_template()
        self._prepared = True

    def _build_template(self) -> None:
        """Migrate one database per server, then clone it for the instances."""
        unique: dict[int, Server] = {id(server): server for server in self.servers.values()}
        for server in unique.values():
            self._drop_quietly(server, self.template)
            server.create_database(self.template)
            env = dict(os.environ, DATABASE_URL=server.url(self.template),
                       SESSION_SECRET="sync-harness-only-session-secret-0123456789",
                       APP_ENV="test")
            step(f"{server.name} migrations (scripts.initialize_database)")
            done = run_tool([sys.executable, "-m", "scripts.initialize_database"],
                            cwd=str(self.repo_root), env=env, timeout=600,
                            label="scripts.initialize_database")
            if done.returncode:
                raise ProvisionError(
                    f"migrations failed on {server.name}: {done.stderr[-1500:]}")

    def _drop_quietly(self, server: Server, database: str) -> None:
        try:
            server.drop_database(database)
        except ProvisionError:
            pass

    def reset(self) -> None:
        """Recreate every instance database from its server's template."""
        if not self._prepared:
            raise ProvisionError("prepare() first")
        for name, server in self.servers.items():
            self._drop_quietly(server, DATABASES[name])
            server.create_database(DATABASES[name], template=self.template)

    def url(self, name: str) -> str:
        return self.servers[name].url(DATABASES[name])

    def urls(self) -> dict[str, str]:
        return {name: self.url(name) for name in INSTANCE_NAMES}

    def server(self, name: str) -> Server:
        return self.servers[name]

    def close(self) -> None:
        for server in {id(s): s for s in self.servers.values()}.values():
            server.close()
