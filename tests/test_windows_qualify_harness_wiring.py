from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PS = (ROOT / "verification/windows_integrated_candidate/qualify_runtime.ps1").read_text()
WF = (ROOT / ".github/workflows/windows-integrated-candidate.yml").read_text()


def _ps_ordering_ok(ps):
    """Oracle for the lifecycle position and exit gate of the harness branch."""
    try:
        conflict = ps.index("if($Probe -and $Harness){throw")
        create = ps.index("CreateAppContainerProfile($profile")
        acl = ps.index("Disposable package RX ACL readback matched")
        branch = ps.index("} elseif($Harness){")
        call = ps.index("qualify_parse_equivalence.py", branch)
        exit_read = ps.index("$harnessExit=$LASTEXITCODE", call)
        gate = ps.index("if($harnessExit -ne 0){throw", exit_read)
        default = ps.index("} else {", gate)
        catch = ps.index("} catch {", default)
        teardown = ps.index("DeleteAppContainerProfile($profile)", catch)
    except ValueError:
        return False
    if not (conflict < create < acl < branch < call < exit_read < gate < default < catch < teardown):
        return False
    between = ps[call:exit_read]
    return between.count("\n") == 1  # exit read is the very next statement after the native call


def test_harness_runs_inside_profile_acl_lifecycle_before_teardown_and_gates_exit():
    assert _ps_ordering_ok(PS)


def test_ordering_oracle_rejects_late_or_ungated_variants():
    assert not _ps_ordering_ok(PS.replace("$harnessExit=$LASTEXITCODE", "$x=1"))
    assert not _ps_ordering_ok(PS.replace("if($harnessExit -ne 0){throw", "if($false){throw"))
    assert not _ps_ordering_ok(PS.replace("if($Probe -and $Harness){throw", "if($false){throw"))
    gap = PS.replace("$harnessExit=$LASTEXITCODE", "Write-Output 'x'\n   $harnessExit=$LASTEXITCODE")
    assert not _ps_ordering_ok(gap)


def test_default_and_probe_branches_are_unchanged_and_harness_is_opt_in():
    assert "[switch]$Probe,[switch]$Harness,[string]$HarnessLock,[string]$HarnessPackagePaths)" in PS
    assert PS.count("run_request.py") == 1 and PS.count("run_probe.py") == 1
    assert "-I \"$PSScriptRoot/run_request.py\"" in PS
    assert "if(-not $HarnessLock -or -not $HarnessPackagePaths){throw" in PS
    assert "$env:PYTHONPATH=$savedPath" in PS and "finally {$env:PYTHONPATH" in PS
    assert "vendor-path metadata" in PS and "trusted_host_vendor_vs_lock" in PS


def _wf_order_ok(wf):
    """Harness must run before the known xls/xlsx loop, inside Invoke-WithLoaderSnaps, so an old workload throw cannot hide it."""
    try:
        snaps = wf.index("Invoke-WithLoaderSnaps {")
        harness = wf.index("QUALIFY_HARNESS=parse-equivalence")
        call = wf.index("-Harness ", harness)
        formats = wf.index("foreach($suffix in @('xls','xlsx'))")
        final = wf.index("Write-Output 'Contained debugger-observed")
    except ValueError:
        return False
    return snaps < harness < call < formats < final and wf.count("-Harness ") == 1


def test_workflow_runs_harness_before_the_known_workload_loop_so_an_old_throw_cannot_hide_it():
    assert 'permissions:\n  contents: read' in WF
    assert _wf_order_ok(WF)
    seg = WF[WF.index("QUALIFY_HARNESS=parse-equivalence"):WF.index("foreach($suffix")]
    assert "ACQUISITION-LOCK.json" in seg and "acquired/packages" in seg
    assert "try" not in seg and "catch" not in seg and "-ErrorAction" not in seg  # nothing swallows a failure


def test_reachability_oracle_rejects_harness_after_the_loop():
    start = WF.index("            Write-Output 'QUALIFY_HARNESS=parse-equivalence'")
    end = WF.index("            foreach($suffix")
    block = WF[start:end]
    after = WF[:start] + WF[end:]
    close = after.index("            }\n            }\n            Write-Output 'Contained")
    moved = after[:close] + "            }\n" + block.rstrip("\n") + "\n" + after[close + len("            }\n"):]
    assert not _wf_order_ok(moved)
