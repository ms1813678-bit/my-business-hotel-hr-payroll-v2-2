#!/usr/bin/env bash
# Runs the environment-gated PostgreSQL acceptance tests that the normal suite skips.
# Each group gets its own freshly migrated, correctly named database, because several tests
# insert fixed fixture rows and some tests refuse any other database name.
# Needs: PostgreSQL 16 server and client tools, PGHOST/PGPORT/PGUSER/PGPASSWORD, SESSION_SECRET.
# Synthetic data only. Not a hotel install and not production activation.
set -uo pipefail
: "${PGHOST:=127.0.0.1}" "${PGPORT:=5432}" "${PGUSER:=hotel_test}"
export PGHOST PGPORT PGUSER
export APP_ENV=test
PY="${PYTHON:-python}"
fail=0
declare -a summary

url() { printf 'postgresql+psycopg://%s:%s@%s:%s/%s' "$PGUSER" "${PGPASSWORD:-}" "$PGHOST" "$PGPORT" "$1"; }

fresh_db() {
  dropdb --if-exists "$1" >/dev/null 2>&1
  createdb "$1" || return 1
  DATABASE_URL="$(url "$1")" "$PY" -m scripts.initialize_database >/dev/null 2>&1
}

# run LABEL DBNAME "ENV=1 ENV2=1" pytest-args...
# KEEP=1 reuses the database left by the previous group (the receipt archive test needs the
# receipt and approval fixture that the approval race test commits).
run() {
  local label="$1" db="$2" envs="$3"; shift 3
  if [ -n "${ONLY:-}" ] && ! [[ "$label" =~ $ONLY ]]; then return; fi
  if [ "${KEEP:-0}" != 1 ]; then
    fresh_db "$db" || { echo "FAIL $label (database setup)"; summary+=("FAIL $label"); fail=1; return; }
  fi
  echo "=== $label (database $db)"
  # shellcheck disable=SC2086
  local out; out="$(mktemp)"
  env DATABASE_URL="$(url "$db")" $envs "$PY" -m pytest -q -p no:cacheprovider "$@" 2>&1 | tee "$out"
  local code="${PIPESTATUS[0]}"
  # A group that skipped anything or ran nothing is a failure: a skipped gate proves nothing.
  local good=0
  if [ "$code" -eq 0 ] && grep -Eq '[0-9]+ passed' "$out" && ! grep -Eq '[0-9]+ skipped' "$out"; then
    good=1
    if grep -Eq '[0-9]+ deselected' "$out" && [ -z "${ALLOW_DESELECT:-}" ]; then good=0; fi
  fi
  if [ "$good" -eq 1 ]; then
    summary+=("ok   $label")
  else
    echo "group failed, skipped tests, or ran no tests: $label (pytest exit $code)"
    summary+=("FAIL $label"); fail=1
  fi
  rm -f "$out"
}

ARCH="MANUAL_ARCHIVE_ROUNDTRIP=1"
run "archive round trip" synthetic_archive_roundtrip "$ARCH" tests/test_actual_archive_roundtrip.py
run "archive overtime chain" synthetic_archive_overtime "$ARCH" tests/test_item2_archive_ot.py
run "rotation source cycle" synthetic_archive_rotation "$ARCH" tests/test_rotation_source_cycle.py
fresh_db synthetic_archive_rotation_target
run "rotated actual CLI" synthetic_archive_rotated_cli "$ARCH ROTATION_TARGET_DATABASE_URL=$(url synthetic_archive_rotation_target)" tests/test_rotated_actual_cli.py

run "synthetic month cycle" synthetic_cycle_month "MANUAL_SYNTHETIC_MONTH_CYCLE=1" tests/test_synthetic_month_cycle.py
run "return register cycle" synthetic_cycle_return "MANUAL_RETURN_REGISTER_CYCLE=1" \
  "tests/test_return_register_cycle.py::test_actual_return_attendance_register_close_next_export"
run "global readiness concurrency" synthetic_cycle_readiness "MANUAL_GLOBAL_READINESS_CONCURRENCY=1" \
  "tests/test_leave_month_close_readiness.py::test_actual_two_sessions_no_shared_to_exclusive_upgrade_deadlock"
run "leave transition concurrency" synthetic_leave_transition "MANUAL_LEAVE_TRANSITION_CONCURRENCY=1" \
  "tests/test_leave_synthetic_transition.py::test_actual_independent_sessions_serialize_replay_and_second_key"
run "approval race" synthetic_cycle_approval_race "MANUAL_APPROVAL_CONCURRENCY=1" \
  "tests/test_reconciliation_approval.py::test_independent_sessions_race"
run "approval lock recovery" synthetic_cycle_approval_lock "MANUAL_APPROVAL_LOCK=1" \
  "tests/test_reconciliation_approval.py::test_actual_lock_timeout_recovery"
run "month review HTTP" synthetic_cycle_month_review "MANUAL_MONTH_REVIEW_HTTP=1" tests/test_month_review_http.py
KEEP=1 run "receipt archive (on the approval race database)" synthetic_cycle_approval_race "MANUAL_RECEIPT_ARCHIVE=1" tests/test_reconciliation_receipt_archive.py

run "shift day HTTP" synthetic_attendance_shift "MANUAL_SHIFT_HTTP=1" tests/test_shift_day_http.py
# The concurrency test seeds with the shift HTTP fixture, so it needs its own fresh database.
run "attendance evidence concurrency" synthetic_attendance_concurrency \
  "MANUAL_SHIFT_HTTP=1 MANUAL_ATTENDANCE_CONCURRENCY=1" tests/test_attendance_evidence_concurrency.py
run "attendance operational HTTP" synthetic_attendance_http "MANUAL_ATTENDANCE_HTTP=1" tests/test_attendance_operational_http.py
ALLOW_DESELECT=1 run "manual opening full path" synthetic_fullpath_main "MANUAL_FULLPATH_ACCEPTANCE=1" tests/test_manual_opening_fullpath.py \
  --deselect tests/test_manual_opening_fullpath.py::test_ot_day_real_main_no_award_or_source_write
run "manual opening overtime day" synthetic_fullpath_ot "MANUAL_FULLPATH_ACCEPTANCE=1" \
  tests/test_manual_opening_fullpath.py::test_ot_day_real_main_no_award_or_source_write
run "report presets" synth "PRESET_QUALIFICATION=1 EXPECT=patched" tests/test_report_preset_pg_qualified.py

echo; echo "=== summary"; printf '%s\n' "${summary[@]}"
exit $fail
