"""Complete migrated CI suite across separately guarded synthetic databases.

No guard changes, selective deselection or hosted-readiness claim. Every test
file belongs to exactly one shard. Existing manual opt-in guards remain intact;
this dispatcher does not enable manual acceptance or certify skipped checks.
Only fresh fixed-name loopback databases are created. Existing DBs are refused.
"""
from pathlib import Path
import os
import subprocess
import sys

CYCLE_FILES=frozenset({'tests/test_return_register_cycle.py'})
# Measured wall seconds per file from hosted Windows runs (maximum over runs ab 45879745669, d2 45886513215,
# b8 45889838148 and 0d50811 45899381498; files >=5s only; others use the default). Four l8 files were
# never reached in any run (session cut by the 900s cap) and carry an ESTIMATE of 90s each.
# The earlier 8e1 table omitted the slow carry/leave/l8 files, so sessions hit the 900s cap.
# Weights only decide where contiguous chunk boundaries fall. Every file still runs
# exactly once, nothing is skipped, and the per-session cap stays 900 seconds.
WINDOWS_SECONDS_8E1={
    'tests/test_a1_department_asof.py':25,
    'tests/test_advance_routes.py':7,
    'tests/test_ai_atomic_corrections.py':22,
    'tests/test_ai_dispatch_http_pg.py':5,
    'tests/test_ai_error_http_pg.py':12,
    'tests/test_ai_pin.py':10,
    'tests/test_ai_plan_model_output.py':22,
    'tests/test_ai_selector_pg.py':11,
    'tests/test_ai_smart_scan.py':9,
    'tests/test_annual_zero_retry_after_growth_independent.py':12,
    'tests/test_attendance_evidence_review.py':25,
    'tests/test_attendance_upload_race_pg.py':29,
    'tests/test_auth_gate_queue_pg.py':6,
    'tests/test_backup_workflow.py':29,
    'tests/test_carry_allocation_retry.py':22,
    'tests/test_carry_availability_compact.py':35,
    'tests/test_carry_consumption_http_pg.py':172,
    'tests/test_carry_consumption_migration_pg.py':25,
    'tests/test_carry_expiry_http_pg.py':128,
    'tests/test_department_preparation_workflow.py':5,
    'tests/test_department_workbook_store.py':6,
    'tests/test_g4_payroll_carry_real_pg.py':19,
    'tests/test_g4_payroll_real_pg_chain.py':9,
    'tests/test_g4_payroll_real_pg_flow.py':12,
    'tests/test_g4_payroll_real_pg_month.py':5,
    'tests/test_g4_reward_types.py':14,
    'tests/test_g8a_monthly_boundary_oracle.py':6,
    'tests/test_g8b_ai_local_only_proof.py':7,
    'tests/test_item2_locale_states.py':6,
    'tests/test_l8_annual_source_adversarial.py':35,
    'tests/test_l8_annual_store_adversarial.py':15,
    'tests/test_l8_carried_balance_cycle_pg.py':23,
    'tests/test_l8_carry_allocation_forgery.py':16,
    'tests/test_l8_carry_availability_extra.py':6,
    'tests/test_l8_carry_consumption_adversarial.py':152,
    'tests/test_l8_cash_out_adversarial.py':6,
    'tests/test_l8_completed_year_adversarial.py':7,
    'tests/test_l8_consumer_v1_bounds_negative.py':47,
    'tests/test_l8_consumption_guard_roles.py':59,
    'tests/test_l8_earlier_year_cash_guard_forgery.py':41,
    'tests/test_l8_expired_cash_backdate.py':8,
    'tests/test_l8_expiry_adversarial.py':97,
    'tests/test_l8_month_correction_pg.py':6,
    'tests/test_l8_next_year_admission_adversarial.py':142,
    'tests/test_l8_source_year_cash_binding.py':69,
    'tests/test_l8_transition_2026_real.py':92,
    'tests/test_l8_transition_stored_retry_negative.py':34,
    'tests/test_l8_year2_closing_consumers.py':50,
    'tests/test_l8_year2_composition.py':29,
    'tests/test_l8_year2_net_owner_adversarial.py':23,
    'tests/test_l8_year2_net_source.py':24,
    'tests/test_l8_year2_post_allocation.py':90,
    'tests/test_l8_year2_post_cash.py':90,
    'tests/test_l8_year2_recorded_source.py':90,
    'tests/test_l8_year3_post_allocation.py':90,
    'tests/test_l8_year_transition_replay.py':57,
    'tests/test_leave_annual_settlement_http_negative_pg.py':12,
    'tests/test_leave_annual_settlement_http_pg.py':30,
    'tests/test_leave_annual_settlement_migration_pg.py':15,
    'tests/test_leave_annual_settlement_source.py':12,
    'tests/test_leave_annual_settlement_store.py':11,
    'tests/test_leave_carry_allocation_store.py':11,
    'tests/test_leave_carry_availability.py':40,
    'tests/test_leave_carry_consumption.py':143,
    'tests/test_leave_carry_consumption_race_pg.py':13,
    'tests/test_leave_carry_expiry.py':189,
    'tests/test_leave_carry_grant.py':10,
    'tests/test_leave_cash_out_approved_item_http_pg.py':6,
    'tests/test_leave_cash_out_http_pg.py':6,
    'tests/test_leave_cash_out_migration_pg.py':10,
    'tests/test_leave_completed_year_source.py':8,
    'tests/test_leave_exact_adjustment.py':14,
    'tests/test_leave_month_close_readiness.py':11,
    'tests/test_leave_month_correction_0083_migration_pg.py':7,
    'tests/test_leave_month_earning.py':7,
    'tests/test_leave_month_earning_guards.py':6,
    'tests/test_leave_next_year_earning.py':15,
    'tests/test_leave_opening_apply.py':21,
    'tests/test_leave_opening_apply_races.py':11,
    'tests/test_leave_opening_receipt_consumers.py':11,
    'tests/test_leave_policy_impact.py':5,
    'tests/test_leave_recorded_source_versions.py':24,
    'tests/test_leave_register_preparation.py':20,
    'tests/test_leave_routes.py':5,
    'tests/test_leave_source_reconciliation.py':6,
    'tests/test_leave_synthetic_transition.py':17,
    'tests/test_leave_workbench_routes.py':5,
    'tests/test_leave_year_transition.py':104,
    'tests/test_leave_zero_decision.py':15,
    'tests/test_login_security.py':10,
    'tests/test_m3c.py':13,
    'tests/test_manual_opening_fullpath.py':13,
    'tests/test_manual_opening_identity_migration.py':12,
    'tests/test_month_earning_http_pg.py':24,
    'tests/test_opening_apply_http_pg.py':14,
    'tests/test_opening_rebind_http_pg.py':22,
    'tests/test_overtime_day_award.py':10,
    'tests/test_overtime_day_concurrency.py':14,
    'tests/test_overtime_day_routes.py':16,
    'tests/test_p2_register_payroll_protection.py':16,
    'tests/test_pay_basis_preview.py':14,
    'tests/test_payroll_guards.py':18,
    'tests/test_payroll_leave_cash_0081_migration_pg.py':8,
    'tests/test_payroll_leave_chain_roundtrip_pg.py':7,
    'tests/test_peer_manual_1003.py':11,
    'tests/test_postgres_migrations.py':10,
    'tests/test_reconciliation_approval.py':36,
    'tests/test_recovery.py':9,
    'tests/test_recovery_hardening.py':5,
    'tests/test_recovery_login_proof.py':7,
    'tests/test_recovery_restore_gate.py':5,
    'tests/test_restore_cli_keyfile.py':9,
    'tests/test_restore_epoch_rebind.py':20,
    'tests/test_restore_interruption.py':11,
    'tests/test_restore_rebind_0080_downgrade_pg.py':16,
    'tests/test_restore_reconciliation.py':8,
    'tests/test_return_register.py':21,
    'tests/test_return_register_http_pg.py':28,
    'tests/test_role_matrix_static.py':5,
    'tests/test_roles.py':20,
    'tests/test_routes_security.py':50,
    'tests/test_security.py':10,
    'tests/test_sync_snapshot_pg.py':15,
    'tests/test_sync_uid_migration.py':10,
    'tests/test_sync_uid_models.py':10,
    'tests/test_sync_uid_roundtrip.py':43,
    'tests/test_telegram_reauth.py':7,
    'tests/test_test_db_guard.py':17,
    'tests/test_triage_fixes_v4.py':5,
    'tests/test_uid_discard_checkpoint_protection.py':7,
    'tests/test_windows_dependency_probe_packet.py':13,
    'tests/test_windows_parse_equivalence_harness.py':6,
    'tests/test_windows_trace_packet.py':6,
    'tests/test_year2_annual_consumer.py':32,
    'tests/test_year2_annual_expiry_shape.py':31,
    'tests/test_year_transition_migration_pg.py':15,
    'tests/test_zero_decision_http_pg.py':45,
    'tests/test_zero_expiry_downgrade_pg.py':11,
    'tests/test_zero_expiry_races_pg.py':83,
    'tests/test_zero_independent_review.py':8,
    'tests/test_zero_ui_owner_adversarial.py':12,
}
DEFAULT_SECONDS=2
CHUNKS=9
LINUX_CHUNKS=5
LINUX_TIMING_FILE=Path(__file__).with_name("ci_linux_seconds.json")


def chunks(files,count=CHUNKS):
    """Contiguous, order-preserving split balanced by measured seconds."""
    files=list(files)
    weights=[WINDOWS_SECONDS_8E1.get(f,DEFAULT_SECONDS) for f in files]
    total=sum(weights)
    out=[[] for _ in range(count)];index=0;seen=0
    for f,weight in zip(files,weights):
        # Move to the next chunk once this chunk has its cumulative share.
        if index<count-1 and out[index] and seen+weight/2>total*(index+1)/count:
            index+=1
        out[index].append(f);seen+=weight
    if any(not group for group in out) or [f for group in out for f in group]!=files:
        raise RuntimeError('Every test file must run exactly once in a non-empty chunk.')
    return out


def linux_chunks(files):
    """Minimax contiguous Linux-only planning weights, not a runtime guarantee.

    Current tracked inventory is the input, never the timing-map membership.
    Unknown future files receive a conservative planning default, not omission.
    The database/migration lifecycle remains outside process partitioning.
    """
    import json
    files=list(files)
    profile=json.loads(LINUX_TIMING_FILE.read_text())
    weights=[profile['seconds'].get(f,30) for f in files]
    if any(type(w) is not int or w<=0 for w in weights):
        raise RuntimeError('Positive integral Linux planning seconds required.')
    count=LINUX_CHUNKS
    if len(files)<count:
        raise RuntimeError('Non-empty Linux sessions required.')
    prefix=[0]
    for w in weights:prefix.append(prefix[-1]+w)
    best=[[float('inf')]*(len(files)+1) for _ in range(count+1)]
    best[0][0]=0;previous={}
    for c in range(1,count+1):
        for j in range(c,len(files)+1):
            value,i=min((max(best[c-1][i],prefix[j]-prefix[i]),i) for i in range(c-1,j))
            best[c][j]=value;previous[c,j]=i
    groups=[];j=len(files)
    for c in range(count,0,-1):
        i=previous[c,j];groups.append(files[i:j]);j=i
    groups.reverse()
    if any(not group for group in groups) or [f for group in groups for f in group]!=files:
        raise RuntimeError('Every test file must run exactly once in a non-empty chunk.')
    return groups


def session_groups(files):
    # Windows retains its existing weights, chunk count and splitting algorithm.
    if len(files)<CHUNKS*10:return [files]
    return linux_chunks(files) if sys.platform.startswith('linux') else chunks(files)


def assignments(root):
    files=sorted(p.relative_to(root).as_posix() for p in (root/'tests').rglob('test_*.py'))
    if not files or not CYCLE_FILES.issubset(files):
        raise RuntimeError('Complete known test inventory required.')
    cycle=[f for f in files if f in CYCLE_FILES]
    leave=[f for f in files if f not in CYCLE_FILES]
    if not leave or not cycle or set(leave)&set(cycle) or sorted(leave+cycle)!=files:
        raise RuntimeError('Every test file must run exactly once.')
    return [('synthetic_leave_ci',leave),('synthetic_cycle_ci',cycle)]


def main():
    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import make_url
    root=Path(__file__).resolve().parents[1]
    url=make_url(os.environ['DATABASE_URL'])
    if (url.drivername!='postgresql+psycopg' or url.host not in {'localhost','127.0.0.1','::1'}
            or url.query):
        raise RuntimeError('Explicit loopback PostgreSQL test source required.')
    plan=assignments(root)
    admin=url.set(database='postgres',drivername='postgresql').render_as_string(hide_password=False)
    # Refuse collisions before doing any work or dropping anything.
    created=[]
    failure=None
    suite_failures=[]
    try:
        with psycopg.connect(admin,autocommit=True) as db:
            for name,_ in plan:
                if db.execute('SELECT 1 FROM pg_database WHERE datname=%s',(name,)).fetchone():
                    raise RuntimeError('Fresh isolated CI databases required; name already exists.')
            for name,_ in plan:
                db.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)));created.append(name)
        for name,files in plan:
            env=dict(os.environ,DATABASE_URL=url.set(database=name).render_as_string(hide_password=False))
            print(f'Full migrated CI shard {name}: {len(files)} test files',flush=True)
            subprocess.run([sys.executable,'-m','scripts.initialize_database'],cwd=root,env=env,check=True,timeout=120)
            groups=session_groups(files)
            for number,group in enumerate(groups,1):
                print(f'  session {number}/{len(groups)}: {len(group)} test files',flush=True)
                try:
                    subprocess.run([sys.executable,'-m','pytest','-v',*group],cwd=root,env=env,check=True,timeout=900)
                except (subprocess.CalledProcessError,subprocess.TimeoutExpired) as exc:
                    # A failing session must not silently leave later chunks unrun.
                    suite_failures.append((name,number,exc))
                    print(f'CI session failed: {name} {number}/{len(groups)} ({type(exc).__name__})',file=sys.stderr,flush=True)
        if suite_failures:
            raise RuntimeError('CI suite failed in sessions: '+', '.join(f'{name}:{number}' for name,number,_ in suite_failures))
    except BaseException as exc:
        failure=exc
        raise
    finally:
        # Drop only the DBs this exact invocation successfully created.
        cleanup_errors=[]
        if created:
            try:
                with psycopg.connect(admin,autocommit=True) as db:
                    for name in reversed(created):
                        try:
                            db.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
                        except Exception:
                            cleanup_errors.append(name)
            except Exception:
                cleanup_errors.extend(name for name in created if name not in cleanup_errors)
        if cleanup_errors:
            message='CI cleanup failed for owned databases: '+', '.join(cleanup_errors)
            if failure is not None:
                print(message,file=sys.stderr)
            else:
                raise RuntimeError(message)


if __name__=='__main__':main()
