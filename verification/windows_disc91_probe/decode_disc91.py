"""Decode the disc91 probe exit code. Parsed from the existing capture line only. Positive-only reading."""
import argparse
import re
import sys

BIT_NAMES=['C1_control_vcruntime140_norefs','C2_control_libffi_full','S8a_python314_full_baseline','V0_checksum_zeroed_only',
 'V1_tls_dir_zeroed','V2_resource_dir_zeroed','V3_entry_stub','V4_tls_resource_entry_combined','S8b_python314_full_repeat']
CLASS_FIELDS=[('S8a',2,9),('V0',3,11),('V1',4,13),('V2',5,15),('V3',6,17),('V4',7,19),('S8b',8,21)]  # name, success bit, shift
CLASS_NAMES=['ok','error5','one_of_1114_126_127_193','other']
NT_NAMES=['ok','C0000022_access_denied','C0000142_dll_init_failed','C0000135_dll_not_found','other']

RULES=[
 'readings_are_positive_only_discriminators_not_causal_claims;valid_only_if_C1_C2_ok_and_S8a_failed_and_V0_failed',
 'V0_ok=checksum_only_copy_loads;signature_policy_or_nondeterminism_finding;no_inference_from_V1_to_V4',
 'V1_ok_with_V0_failed=tls_directory_entry_is_needed_for_the_failure',
 'V2_ok_with_V0_failed=resource_directory_entry_(manifest)_is_needed_for_the_failure',
 'V3_ok_with_V0_failed=entry_point_code_(DllMain)_is_needed_for_the_failure',
 'V4_ok_while_V1_V2_V3_fail=combination_needed',
 'V4_fails_with_V0_failed=none_of_tls_resource_entry_alone_or_together_explains_the_failure',
 'all_variants_also_have_the_checksum_zeroed_and_every_modified_copy_has_an_invalid_authenticode_hash_except_V0',
 'NTSTATUS_class_is_from_ntdll_LdrLoadDll_on_python314_dll_baseline_only',
 'every_step_runs_after_an_earlier_failure;later_results_are_not_causal;order_C1_C2_S8a_LDR_V0_V3_V4_V2_S8b_V1',
 'exit_0x20000000_exactly=probe_refused_path_construction;no_result',
 'raw_exit_code_outside_probe_range_or_inconsistent=crash_or_exception;report_raw_do_not_decode',
 'the_old_no_further_instrument_line_is_superseded_by_per_run_approval_for_disposable_windows_ci;each_probe_needs_a_reviewed_design',
]


def decode(code):
    if type(code) is not int or not 0x20000000<=code<=0x3FFFFFFF:raise ValueError('Not a disc91 exit code')
    if code>>29!=1:raise ValueError('Reserved bits set')
    if code==0x20000000:return {'refused':True}
    if (code>>26)&7:raise ValueError('Reserved bits 26-28 set')
    bits=[(code>>i)&1 for i in range(9)]
    classes={n:(code>>sh)&3 for n,_b,sh in CLASS_FIELDS}
    nt=(code>>23)&7
    if nt>4:raise ValueError('NTSTATUS class out of range')
    for name,b,_sh in CLASS_FIELDS:
        if (classes[name]==0)!=bool(bits[b]):raise ValueError('Class inconsistent with step bit '+name)
    return {'refused':False,'bits':bits,'classes':classes,'nt':nt}


def readings(d):
    b=d['bits'];out=[]
    if not (b[0] and b[1]):return ['controls_failed_readings_invalid']
    if b[2]:return ['baseline_not_reproduced_readings_invalid']
    if b[3]:
        return ['V0_passed_checksum_only_copy_loads;finding_on_signature_or_policy_or_nondeterminism;no_inference_from_V1_to_V4']
    if b[4]:out.append('V1_tls_directory_zeroed_loads')
    if b[5]:out.append('V2_resource_directory_zeroed_loads')
    if b[6]:out.append('V3_entry_stub_loads')
    if b[7] and not (b[4] or b[5] or b[6]):out.append('V4_combination_loads_while_singles_fail')
    elif b[7]:out.append('V4_loads')
    if not b[7]:out.append('V4_fails_none_of_the_three_alone_or_together')
    return out


def main():
    p=argparse.ArgumentParser();p.add_argument('--exit',required=True);a=p.parse_args()
    if not re.fullmatch(r'[0-9]{1,10}',a.exit):raise ValueError('Exit code format')
    d=decode(int(a.exit))
    if d['refused']:
        print('disc91_probe_refused path_construction=1 no_result=1');return
    for i,n in enumerate(BIT_NAMES):print('disc91_step name=%s ok=%d'%(n,d['bits'][i]))
    for n,_b,_s in CLASS_FIELDS:print('disc91_class step=%s class=%s'%(n,CLASS_NAMES[d['classes'][n]]))
    print('disc91_ldr_ntstatus class=%s'%NT_NAMES[d['nt']])
    for r in readings(d):print('disc91_reading '+r)
    for rule in RULES:print('disc91_rule '+rule)
    print('disc91_limits no_python_code_run=1 no_Py_Initialize=1 diagnostic_only=1 negative_inconclusive=1 security_acceptance=NOT_ESTABLISHED')


if __name__=='__main__':
    try:main()
    except ValueError as exc:
        raw=sys.argv[sys.argv.index('--exit')+1] if '--exit' in sys.argv[:-1] else 'unknown'
        print('disc91_decode_refused raw_exit='+(raw if re.fullmatch(r'[0-9]{1,10}',raw) else 'unparsed')+' crash_or_out_of_range_report_raw_not_decoded=1 reason='+str(exc)[:60]);sys.exit(2)
