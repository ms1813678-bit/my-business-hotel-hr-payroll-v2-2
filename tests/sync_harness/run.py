"""Run the multi-instance sync scenarios.

    python -m tests.sync_harness.run                     # every scenario, reference engine
    python -m tests.sync_harness.run --scenario replay --seed 7 --keep
    python -m tests.sync_harness.run --engine app        # once app.sync_session exists

Exit code 0 only when every check passed. The report (JSON) names the engine, the
platform, every scenario, every check and the random seed, so a failure can be
repeated exactly.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

from . import provision, scenarios


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tests.sync_harness.run")
    parser.add_argument("--scenario", action="append", default=None,
                        help="scenario name, repeatable; default: every scenario")
    parser.add_argument("--list", action="store_true", help="print scenario names and exit")
    parser.add_argument("--seed", type=int, default=20261127, help="seed for randomized runs")
    parser.add_argument("--random-steps", type=int, default=18)
    parser.add_argument("--engine", default="auto", choices=("auto", "reference", "app"))
    parser.add_argument("--work-dir", default=None)
    parser.add_argument("--keep", action="store_true", help="keep databases, logs and dumps")
    parser.add_argument("--pg-external", action="store_true",
                        help="use PGHOST/PGPORT/PGUSER instead of embedded PostgreSQL")
    parser.add_argument("--pg-bin", default=None, help="directory holding initdb/pg_ctl/psql")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--quiet", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list:
        for name, _ in scenarios.SCENARIOS:
            print(name)
        return 0

    selected = scenarios.SCENARIOS
    if args.scenario:
        wanted = set(args.scenario)
        unknown = wanted - {name for name, _ in scenarios.SCENARIOS}
        if unknown:
            print(f"unknown scenario(s): {sorted(unknown)}", file=sys.stderr)
            return 2
        selected = [(name, func) for name, func in scenarios.SCENARIOS if name in wanted]

    work_dir = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="sync-harness-"))
    work_dir.mkdir(parents=True, exist_ok=True)
    provisioner = provision.Provisioner(work_dir, external=args.pg_external, pg_bin=args.pg_bin)
    report = {
        "started": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "engine_requested": args.engine,
        "seed": args.seed,
        "work_dir": str(work_dir),
        "scenarios": [],
    }
    print(f"seed={args.seed} work_dir={work_dir}", flush=True)
    started = time.time()
    try:
        provisioner.prepare()
        report["postgres"] = _postgres_version(provisioner)
        for name, func in selected:
            result = _run_scenario(provisioner, work_dir, name, func, args)
            report["scenarios"].append(result)
            status = "PASS" if result["status"] == "pass" else "FAIL"
            print(f"[{status}] {name} ({result['duration_s']:.1f}s, "
                  f"{len(result['checks'])} checks)", flush=True)
            if result["status"] != "pass":
                for failure in result["failures"]:
                    print(f"    - {failure['check']}: {failure['detail'][:400]}", flush=True)
    finally:
        provisioner.close()
        report["duration_s"] = round(time.time() - started, 1)
        report["engine_used"] = _engine_used(report)
        json_out = Path(args.json_out) if args.json_out else work_dir / "report.json"
        json_out.write_text(json.dumps(report, indent=2, default=str))
        print(f"report: {json_out}", flush=True)
        if not args.keep:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            print(f"kept: {work_dir}", flush=True)

    failed = [item for item in report["scenarios"] if item["status"] != "pass"]
    print(f"{len(report['scenarios']) - len(failed)}/{len(report['scenarios'])} scenarios passed",
          flush=True)
    return 0 if not failed else 1


def _engine_used(report: dict) -> str:
    engines = {item.get("engine") for item in report["scenarios"] if item.get("engine")}
    if not engines:
        return report["engine_requested"]
    if len(engines) == 1:
        return engines.pop()
    return ",".join(sorted(engines))


def _postgres_version(provisioner: provision.Provisioner) -> str:
    try:
        server = provisioner.server("A")
        return server.sql("postgres", "SELECT version()").strip().splitlines()[2].strip()
    except Exception as exc:  # pragma: no cover - informational only
        return f"unknown ({exc})"


def _run_scenario(provisioner: provision.Provisioner, work_dir: Path, name: str, func,
                  args) -> dict:
    ctx = scenarios.Context(provisioner, work_dir / "scenarios" / name, engine=args.engine,
                            seed=args.seed)
    ctx.notes["random_steps"] = args.random_steps
    ctx.notes["trace"] = bool(__import__("os").environ.get("SYNC_TRACE"))
    checks = scenarios.Checks()
    started = time.time()
    error = ""
    engine_used = ""
    try:
        ctx.start()
        func(ctx, checks)
        engine_used = ctx.client("A").health()["engine"]
    except Exception:
        error = traceback.format_exc()
        checks.check("scenario ran to the end", False, error)
    finally:
        try:
            ctx.close()
        except Exception:
            pass
    failures = checks.failures()
    return {
        "name": name,
        "status": "pass" if not failures else "fail",
        "engine": engine_used,
        "duration_s": round(time.time() - started, 1),
        "checks": checks.items,
        "failures": failures,
        "error": error[-4000:] if error else "",
    }


if __name__ == "__main__":
    raise SystemExit(main())
