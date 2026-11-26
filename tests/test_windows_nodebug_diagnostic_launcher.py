"""Source-text guards for the DIAGNOSTIC no-debugger launcher copy.

Text only. Nothing here compiles or runs C, and nothing here is Windows or MSVC evidence.
The copy is never production and never feeds acceptance.
"""
import difflib
import re
from pathlib import Path

D = Path(__file__).resolve().parents[1] / "verification" / "windows_integrated_candidate"
BASE = (D / "launcher.c").read_text(encoding="utf-8").splitlines()
COPY_TEXT = (D.parent / "windows_nodebug_diagnostic" / "launcher_nodebug_diag.c").read_text(encoding="utf-8")
COPY = COPY_TEXT.splitlines()
CODE_TEXT = "\n".join(l for l in COPY if not l.lstrip().startswith(("/*", "*", "//")))

DEBUG_TOKENS = ("DEBUG_ONLY_THIS_PROCESS", "WaitForDebugEventEx", "ContinueDebugEvent", "loader_debug",
                "debug.", "held", "event_held", "DEBUG_EVENT")
# Controls that must survive in the copy exactly as in launcher.c.
RETAINED = (
    "CREATE_SUSPENDED", "SECURITY_CAPABILITIES", "ALL_APPLICATION_PACKAGES_POLICY", "PROC_THREAD_ATTRIBUTE_JOB_LIST",
    "PROC_THREAD_ATTRIBUTE_HANDLE_LIST", "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE", "LOCAL" "APPDATA=%ls", "SYSTEMROOT=%ls",
    'stage="verify_before_resume"', "verify_created_child(pi.hProcess,job,sid,1)", "diagnose_startup(", "diagnose_real_open(",
    "GetTickCount64()+3000", "TerminateJobObject(job,99)", "CancelSynchronousIo", "security_acceptance=NOT_ESTABLISHED",
    "code!=0", "prior!=1", "-I -B -X utf8",
)


def _changes():
    removed, added = [], []
    for line in difflib.unified_diff(BASE, COPY, lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        (removed if line[0] == "-" else added).append(line[1:])
    return removed, added


def test_copy_is_labelled_diagnostic_and_not_production():
    head = "\n".join(COPY[:4])
    assert "DIAGNOSTIC COPY ONLY: NO DEBUGGER" in head and "never acceptance" in head


def test_debugger_pieces_are_absent():
    for token in DEBUG_TOKENS:
        for line in COPY:
            if token in line:
                # only labels may mention the word, never code paths
                assert line.lstrip().startswith(("/*", "*", "//")) or "debugger_attached=0" in line or "held_" in line and "absent" in line, (token, line)
    for t in ("DEBUG_ONLY_THIS_PROCESS", "WaitForDebugEvent", "ContinueDebugEvent", "loader_debug", "!debug.exited", "event_held", "DEBUG_EVENT"):
        assert t not in CODE_TEXT, t


def test_every_retained_control_is_in_both_files():
    for token in RETAINED:
        assert token in "\n".join(BASE), token
        assert token in COPY_TEXT, token


def test_removed_lines_are_only_debug_relation_lines():
    removed, _ = _changes()
    allowed = re.compile("|".join(re.escape(t) for t in DEBUG_TOKENS + ("loader_debug_", "Debug relation", "Advisory function return", "LOCAL CANDIDATE ONLY", "CANDIDATE_RESUMED", "verify_after_attach_held_event", "hold_initial_debug_event", "resume_before_first_creation_event", "create_suspended", "ResumeThread", "prior!=1", "loader_debug_resume")))
    for line in removed:
        assert line.strip() in ("", "}") or allowed.search(line), line
    # the resume statement is moved, not dropped, and the prior_suspend_count==1 gate is kept
    assert "ResumeThread(pi.hThread)" in COPY_TEXT
    assert not any("verify_before_resume" in l for l in removed)
    assert not any("TerminateJobObject" in l for l in removed)


def test_suspended_verification_precedes_every_probe_and_resume():
    i_verify = COPY_TEXT.index('stage="verify_before_resume"')
    for marker in ("diagnose_startup(", "diagnose_real_open(", "ResumeThread("):
        assert i_verify < COPY_TEXT.index(marker), marker
    assert COPY_TEXT.index("diagnose_real_open(") < COPY_TEXT.index("ResumeThread(")
    assert COPY_TEXT.count("ResumeThread(") == 1
    assert COPY_TEXT.index("start_reader") < COPY_TEXT.index("ResumeThread(")


def test_post_resume_check_is_labelled_best_effort_and_missing_guarantee_is_explicit():
    assert "held_event_verification=absent" in COPY_TEXT
    assert "held_create_event_guarantee=absent" in COPY_TEXT
    assert "not_acceptance=1" in COPY_TEXT
    assert "STILL_ACTIVE" in COPY_TEXT and "skipped_child_exited" in COPY_TEXT and "refused_or_raced" in COPY_TEXT
    assert "observation_only=1 fatal=0" in COPY_TEXT and "Post-resume observation ONLY" in COPY_TEXT
    # post-resume verify is never fatal: no goto done within the observation block
    blk = COPY_TEXT[COPY_TEXT.index("Post-resume observation ONLY"):COPY_TEXT.index('stage="duplex_wait"')]
    assert "goto done" not in blk and "stage=" not in blk


def test_create_process_flags_differ_only_by_debug_flag():
    def flags(text):
        m = re.search(r"CreateProcessW\([^;]*?TRUE,([A-Z_|]+),environment", text)
        assert m
        return m.group(1).split("|")
    base, copy = flags("\n".join(BASE)), flags(COPY_TEXT)
    assert [f for f in base if f != "DEBUG_ONLY_THIS_PROCESS"] == copy


def test_fixed_vocabulary_lines_only_use_fixed_fields():
    for m in re.finditer(r'"((?:diag_nodebug_|DIAG_NODEBUG_)[^"]*)', COPY_TEXT):
        text = m.group(1)
        assert "%ls" not in text and text.count("%s") <= (1 if "post_resume_observation status=%s " in text else 0), text


def test_header_lists_order_and_timing_factors_and_makes_no_debugger_only_claim():
    head = "\n".join(COPY[:12])
    assert "Differs only" not in COPY_TEXT
    for phrase in ("BEFORE ResumeThread", "feeder and reader", "second verification", "not a whole-launch deadline", "cannot be attributed"):
        assert phrase in head, phrase


def test_begin_marker_immediately_precedes_post_resume_verify_and_is_nonfatal():
    i = COPY_TEXT.index("diag_nodebug_post_resume_observation_begin")
    j = COPY_TEXT.index("verify_created_child(pi.hProcess,job,sid,1)", i)
    assert "lines_until_status_are_post_resume_not_suspended=1" in COPY_TEXT
    assert COPY_TEXT[i:j].count("verify_created_child") == 0 and j - i < 220
    assert COPY_TEXT.index("verify_before_resume") < COPY_TEXT.index("ResumeThread(") < i
