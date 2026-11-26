"""Decode the startup-probe exit code. Parsed from the existing capture line only."""
import argparse
import re
import sys

STEPS=['loadlibrary_python314_with_imports_and_dllmain','getprocaddress_Py_GetVersion','call_Py_GetVersion_no_init',
       'bcryptgenrandom','read_python314_zip','read_python314_pth','read_candidate_worker','stdin_file_type','stdout_file_type','stderr_file_type']


RULES=[
 'all_10_ok=loader_resolution_DllMain_Py_GetVersion_BCryptGenRandom_three_reads_and_std_handle_types_work_in_container;remaining_unknown=python_exe_image_properties_manifest_signing_policy_args_env_and_Python_init_itself;not_acceptance',
 'step1_fail=loader_level_failure_with_recorded_error;the_failing_dependency_is_NOT_named',
 'step2_or_3_fail_with_step1_ok=export_or_call_issue_not_file_access',
 'step4_fail=CNG_random_path;error_is_NTSTATUS_low15',
 'steps5_to_7_fail=file_access_in_stage_dir;error_distinguishes_not_found_vs_denied_vs_sharing',
 'steps8_to_10_fail=std_handle_setup',
 'raw_exit_code_outside_probe_range=crash_or_exception;report_raw_do_not_decode',
 'first_failing_step_is_first_break_in_order;later_failures_are_not_causal',
]


def decode(code):
    if type(code) is not int or not 0x20000000<=code<=0x3FFFFFFF:raise ValueError('Not a startup probe exit code')
    if code>>29!=1:raise ValueError('Reserved bits set')
    mask=code&0x3FF;first=(code>>10)&0xF;err=(code>>14)&0x7FFF
    if first>10:raise ValueError('First-failing step out of range')
    rows=[{'step':i+1,'name':n,'ok':int(bool(mask>>i&1))} for i,n in enumerate(STEPS)]
    if (first==0)!=(mask==0x3FF):raise ValueError('First-failing field inconsistent with mask')
    if first and rows[first-1]['ok']:raise ValueError('First-failing step marked successful')
    return rows,first,err


def main():
    p=argparse.ArgumentParser();p.add_argument('--exit',required=True);a=p.parse_args()
    if not re.fullmatch(r'[0-9]{1,10}',a.exit):raise ValueError('Exit code format')
    rows,first,err=decode(int(a.exit))
    for r in rows:print('startup_step step=%d name=%s ok=%d'%(r['step'],r['name'],r['ok']))
    print('startup_first_fail step=%d error_low15=%d error_is_getlasterror_except_steps_3_4_which_are_ntstatus_low15=1'%(first,err))
    for rule in RULES:print('startup_rule '+rule)
    print('startup_limits no_python_code_run=1 no_Py_Initialize=1 error_truncated_to_15_bits=1 reason_for_later_steps_not_inferred=1 diagnostic_only=1 negative_inconclusive=1 security_acceptance=NOT_ESTABLISHED')


if __name__=='__main__':
    try:main()
    except ValueError as exc:
        raw=sys.argv[sys.argv.index('--exit')+1] if '--exit' in sys.argv[:-1] else 'unknown'
        print('startup_decode_refused raw_exit='+(raw if re.fullmatch(r'[0-9]{1,10}',raw) else 'unparsed')+' crash_or_out_of_range_report_raw_not_decoded=1 reason='+str(exc)[:60]);sys.exit(2)
