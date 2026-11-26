"""Decode the disc92 probe exit code. Parsed from the existing capture line only. Positive-only reading."""
import argparse
import re
import sys

BIT_NAMES=['C1_control_vcruntime140_norefs','C2_control_libffi_full','S8a_python314_full_baseline','V0_checksum_zeroed_only',
 'A_manifest_dependency_element_blanked','B_manifest_comment_char_control','C_version_resource_zeroed','S8b_python314_full_repeat']
CLASS_FIELDS=[('S8a',2,8),('V0',3,10),('A',4,12),('B',5,14),('C',6,16),('S8b',7,18)]  # name, success bit, shift
CLASS_NAMES=['ok','error5','one_of_1114_126_127_193','other']
NT_NAMES=['ok','C0000022_access_denied','C0000142_dll_init_failed','C0000135_dll_not_found','other']

RULES=[
 'readings_are_positive_only_discriminators_not_causal_claims;valid_only_if_C1_C2_ok_and_S8a_failed_and_V0_failed',
 'V0_ok=checksum_only_copy_loads;signature_policy_or_nondeterminism_finding;no_inference_from_A_B_C',
 'B_ok=comment_char_control_loads;A_not_interpretable;nondeterminism_or_content_sensitivity',
 'A_ok_and_B_failed=manifest_dependency_element_is_needed_for_the_failure',
 'C_ok_with_V0_failed=version_resource_is_needed_for_the_failure',
 'A_failed_and_C_failed_with_B_failed=neither_explains_the_failure;stop_and_return_to_owner',
 'every_modified_copy_has_an_invalid_authenticode_hash_except_V0;checksum_zeroed_in_all',
 'NTSTATUS_class_is_from_ntdll_LdrLoadDll_on_python314_dll_baseline_only',
 'every_step_runs_after_an_earlier_failure;later_results_are_not_causal;order_C1_C2_S8a_LDR_V0_B_S8b_C_A',
 'exit_0x20000000_exactly=probe_refused_path_construction;no_result',
 'raw_exit_code_outside_probe_range_or_inconsistent=crash_or_exception;report_raw_do_not_decode',
]


def decode(code):
    if type(code) is not int or not 0x20000000<=code<=0x3FFFFFFF:raise ValueError('Not a disc92 exit code')
    if code>>29!=1:raise ValueError('Reserved bits set')
    if code==0x20000000:return {'refused':True}
    if (code>>23)&0x3f:raise ValueError('Reserved bits 23-28 set')
    bits=[(code>>i)&1 for i in range(8)]
    classes={n:(code>>sh)&3 for n,_b,sh in CLASS_FIELDS}
    nt=(code>>20)&7
    if nt>4:raise ValueError('NTSTATUS class out of range')
    for name,b,_sh in CLASS_FIELDS:
        if (classes[name]==0)!=bool(bits[b]):raise ValueError('Class inconsistent with step bit '+name)
    return {'refused':False,'bits':bits,'classes':classes,'nt':nt}


def readings(d):
    b=d['bits'];out=[]
    if not (b[0] and b[1]):return ['controls_failed_readings_invalid']
    if b[2]:return ['baseline_not_reproduced_readings_invalid']
    if b[3]:
        return ['V0_passed_checksum_only_copy_loads;finding_on_signature_or_policy_or_nondeterminism;no_inference_from_A_B_C']
    if b[5]:
        out.append('B_control_loads_A_not_interpretable')
        if b[6]:out.append('C_version_zeroed_loads')
        return out
    if b[4]:out.append('A_manifest_dependency_element_blanked_loads_B_failed_dependency_element_needed')
    if b[6]:out.append('C_version_resource_zeroed_loads')
    if not b[4] and not b[6]:out.append('A_and_C_fail_neither_explains_stop_return_to_owner')
    elif not b[4]:out.append('A_fails')
    elif not b[6]:out.append('C_fails')
    return out


def main():
    p=argparse.ArgumentParser();p.add_argument('--exit',required=True);a=p.parse_args()
    if not re.fullmatch(r'[0-9]{1,10}',a.exit):raise ValueError('Exit code format')
    d=decode(int(a.exit))
    if d['refused']:
        print('disc92_probe_refused path_construction=1 no_result=1');return
    for i,n in enumerate(BIT_NAMES):print('disc92_step name=%s ok=%d'%(n,d['bits'][i]))
    for n,_b,_s in CLASS_FIELDS:print('disc92_class step=%s class=%s'%(n,CLASS_NAMES[d['classes'][n]]))
    print('disc92_ldr_ntstatus class=%s'%NT_NAMES[d['nt']])
    for r in readings(d):print('disc92_reading '+r)
    for rule in RULES:print('disc92_rule '+rule)
    print('disc92_limits no_python_code_run=1 no_Py_Initialize=1 diagnostic_only=1 negative_inconclusive=1 security_acceptance=NOT_ESTABLISHED')


if __name__=='__main__':
    try:main()
    except ValueError as exc:
        raw=sys.argv[sys.argv.index('--exit')+1] if '--exit' in sys.argv[:-1] else 'unknown'
        print('disc92_decode_refused raw_exit='+(raw if re.fullmatch(r'[0-9]{1,10}',raw) else 'unparsed')+' crash_or_out_of_range_report_raw_not_decoded=1 reason='+str(exc)[:60]);sys.exit(2)
