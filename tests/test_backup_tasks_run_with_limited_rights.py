"""Backup tasks must run as the installing user with limited rights, never SYSTEM/HIGHEST (Mo, 27 Nov 2026)."""
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" / "windows" / "install_backup_tasks.ps1").read_text(encoding="utf-8")


def test_tasks_are_not_system_or_highest():
    code = "\n".join(line for line in SCRIPT.splitlines() if not line.lstrip().startswith("#"))
    assert '"SYSTEM"' not in code and "HIGHEST" not in code.upper().replace("LIMITED", "")
    assert '"/RU", $RunAsUser' in code and '"/RL", "LIMITED"' in code and '"/IT"' in code
    assert "IsSystem" in code


def test_missed_runs_catch_up_and_battery_does_not_block():
    code = "\n".join(line for line in SCRIPT.splitlines() if not line.lstrip().startswith("#"))
    assert '@("StartWhenAvailable", "true")' in code
    assert '@("DisallowStartIfOnBatteries", "false")' in code and '@("StopIfGoingOnBatteries", "false")' in code
    assert "/XML" in code and "Set-ScheduledTask" not in code


def test_least_privilege_is_written_into_the_task_xml():
    code = "\n".join(line for line in SCRIPT.splitlines() if not line.lstrip().startswith("#"))
    assert '$RunLevelNode.InnerText = "LeastPrivilege"' in code
