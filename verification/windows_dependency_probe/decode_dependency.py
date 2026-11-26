"""Decode the dependency-probe exit code. Parsed from the existing capture line only."""
import argparse
import re
import sys

STEPS=['version.dll','ws2_32.dll','advapi32.dll','vcruntime140.dll','api-ms-win-crt-runtime-l1-1-0.dll',
       'api-ms-win-core-path-l1-1-0.dll','other_11_crt_api_sets','python314.dll_last']
EXTRA=['convert','environment','filesystem','heap','locale','math','process','stdio','string','time','conio']

RULES=[
 'all_8_ok=every_direct_dependency_and_python314_load_with_full_resolution_in_container;the_earlier_error5_did_not_reproduce;not_acceptance',
 'only_step8_fails_all_top_level_loads_ok=failure_is_python314_DllMain_or_own_init_or_resolution_detail_not_a_named_direct_dependency',
 'a_step_1_to_7_fails=that_name_is_the_first_failing_top_level_dependency;its_own_subtree_is_NOT_named;step8_failure_after_it_is_not_independently_explained',
 'step_1_to_6_fail_names_only_the_top_level_dependency;its_transitive_imports_are_NOT_named',
 'step7_fail=first_failing_extra_crt_set_named_by_index;other_extra_sets_after_it_unreported',
 'every_step_runs_after_an_earlier_failure;first_failing_step_is_first_in_order;later_failures_are_not_causal',
 'error_is_GetLastError_low13_bits;above_8191_is_truncated',
 'raw_exit_code_outside_probe_range=crash_or_exception;report_raw_do_not_decode',
 'no_further_instrument_after_this_run;next_step_is_an_owner_architecture_decision_with_options',
]


def decode(code):
    if type(code) is not int or not 0x20000000<=code<=0x3FFFFFFF:raise ValueError('Not a dependency probe exit code')
    if code>>29!=1:raise ValueError('Reserved bits set')
    mask=code&0xFF;first=(code>>8)&0xF;err=(code>>12)&0x1FFF;extra=(code>>25)&0xF
    if first>8:raise ValueError('First-failing step out of range')
    if extra>11:raise ValueError('Extra index out of range')
    rows=[{'step':i+1,'name':n,'ok':int(bool(mask>>i&1))} for i,n in enumerate(STEPS)]
    if (first==0)!=(mask==0xFF):raise ValueError('First-failing field inconsistent with mask')
    if first and rows[first-1]['ok']:raise ValueError('First-failing step marked successful')
    if extra and first!=7:raise ValueError('Extra index without step 7 failure')
    if first==7 and extra==0:raise ValueError('Step 7 failure without extra index')
    if first==0 and err:raise ValueError('Error without failure')
    return rows,first,err,extra


def main():
    p=argparse.ArgumentParser();p.add_argument('--exit',required=True);a=p.parse_args()
    if not re.fullmatch(r'[0-9]{1,10}',a.exit):raise ValueError('Exit code format')
    rows,first,err,extra=decode(int(a.exit))
    for r in rows:print('dependency_step step=%d name=%s ok=%d'%(r['step'],r['name'],r['ok']))
    print('dependency_first_fail step=%d error_low13=%d extra_crt_set=%s'%(first,err,EXTRA[extra-1] if extra else 'none'))
    for rule in RULES:print('dependency_rule '+rule)
    print('dependency_limits no_python_code_run=1 no_Py_Initialize=1 subtree_of_failing_name_not_named=1 diagnostic_only=1 negative_inconclusive=1 security_acceptance=NOT_ESTABLISHED')


if __name__=='__main__':
    try:main()
    except ValueError as exc:
        raw=sys.argv[sys.argv.index('--exit')+1] if '--exit' in sys.argv[:-1] else 'unknown'
        print('dependency_decode_refused raw_exit='+(raw if re.fullmatch(r'[0-9]{1,10}',raw) else 'unparsed')+' crash_or_out_of_range_report_raw_not_decoded=1 reason='+str(exc)[:60]);sys.exit(2)
