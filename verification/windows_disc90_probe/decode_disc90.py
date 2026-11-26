"""Decode the disc90 probe exit code. Parsed from the existing capture line only. Positive-only reading."""
import argparse
import re
import sys

BIT_NAMES=['C1_control_vcruntime140_norefs','C2_control_libffi_full','S2_python314_as_datafile','S3_python314_as_image_resource',
 'S4_python314_dont_resolve_refs','S6a_both_security_readable','S6b_dacl_bytes_equal','S6c_owner_equal','S6d_python314_has_label_ace',
 'S6e_label_state_equal','S6f_createfile_exec_read_ok','S8a_python314_full_baseline','S8b_python314_full_after_preloads','S5_renamed_copy_full']
CLASS_FIELDS=[('S2',2,14),('S3',3,16),('S4',4,18),('S8a',11,20),('S5',13,22)]  # name, success bit, shift
CLASS_NAMES=['ok','error5','one_of_1114_126_127_193','other']
IMPORTS=['VERSION.dll','WS2_32.dll','api-ms-win-core-path-l1-1-0.dll','bcrypt.dll','ADVAPI32.dll','KERNEL32.dll','VCRUNTIME140.dll',
 'api-ms-win-crt-stdio-l1-1-0.dll','api-ms-win-crt-environment-l1-1-0.dll','api-ms-win-crt-string-l1-1-0.dll','api-ms-win-crt-runtime-l1-1-0.dll',
 'api-ms-win-crt-heap-l1-1-0.dll','api-ms-win-crt-locale-l1-1-0.dll','api-ms-win-crt-convert-l1-1-0.dll','api-ms-win-crt-math-l1-1-0.dll',
 'api-ms-win-crt-time-l1-1-0.dll','api-ms-win-crt-process-l1-1-0.dll','api-ms-win-crt-conio-l1-1-0.dll','api-ms-win-crt-filesystem-l1-1-0.dll']

RULES=[
 'readings_are_positive_only_discriminators_not_causal_claims;valid_only_if_C1_and_C2_ok',
 'R1_S2_fails_error5=file_read_of_python314_dll_data_denied',
 'R2_S2_ok_S3_fails=image_section_creation_denied',
 'R3_S2_S3_ok_S4_fails_error5=executable_image_mapping_denied;compare_S6_bits',
 'R4_S4_ok_S8a_fails=failure_after_mapping(imports_TLS_DllMain_manifest);S7_ok_and_S8b_ok=import_order_effect;S8b_fails=content_level_after_mapping',
 'R5_S8a_ok=baseline_failure_not_reproduced',
 'R6_S5_ok_while_S8b_fails=problem_is_the_original_file_object_not_its_bytes',
 'S6_bits_are_security_descriptor_comparisons_between_python314_dll_and_vcruntime140_dll;no_descriptor_content_printed',
 'every_step_runs_after_an_earlier_failure;later_results_are_not_causal',
 'exit_0x20000000_exactly=probe_refused_path_construction;no_result',
 'raw_exit_code_outside_probe_range_or_inconsistent=crash_or_exception;report_raw_do_not_decode',
 'no_further_instrument_after_this_run;next_step_is_an_owner_architecture_decision_with_options',
]


def decode(code):
    if type(code) is not int or not 0x20000000<=code<=0x3FFFFFFF:raise ValueError('Not a disc90 exit code')
    if code>>29!=1:raise ValueError('Reserved bits set')
    if code==0x20000000:return {'refused':True}
    bits=[(code>>i)&1 for i in range(14)]
    classes={n:(code>>sh)&3 for n,_b,sh in CLASS_FIELDS}
    index=(code>>24)&0x1F
    if index>19:raise ValueError('Import index out of range')
    for name,b,_sh in CLASS_FIELDS:
        if (classes[name]==0)!=bool(bits[b]):raise ValueError('Class inconsistent with step bit '+name)
    if not bits[5] and any(bits[i] for i in (6,7,8,9)):raise ValueError('S6 detail bits without S6a')
    if bits[11] and not bits[12]:raise ValueError('S8b failed although S8a loaded the image')
    return {'refused':False,'bits':bits,'classes':classes,'s7_first_fail':index}


def readings(d):
    b=d['bits'];c=d['classes'];out=[]
    if not (b[0] and b[1]):return ['controls_failed_readings_invalid']
    if not b[2]:
        out.append('R1 file_read_denied_error5' if c['S2']==1 else 'R1x S2_failed_class=%s'%CLASS_NAMES[c['S2']])
    elif not b[3]:out.append('R2 image_section_creation_failed')
    elif not b[4]:out.append('R3 executable_mapping_failed_error5' if c['S4']==1 else 'R3x S4_failed_class=%s'%CLASS_NAMES[c['S4']])
    elif not b[11]:
        if d['s7_first_fail']==0 and b[12]:out.append('R4 after_mapping_failure;S7_ok_and_S8b_ok=import_order_effect')
        elif not b[12]:out.append('R4 after_mapping_failure;S8b_fails=content_level')
        else:out.append('R4 after_mapping_failure;S7_import_failed_first_index=%d'%d['s7_first_fail'])
    if b[11]:out.append('R5 baseline_failure_not_reproduced')
    if b[13] and not b[12]:out.append('R6 original_file_object_problem')
    if not out:out.append('no_reading_matched_report_raw')
    return out


def main():
    p=argparse.ArgumentParser();p.add_argument('--exit',required=True);a=p.parse_args()
    if not re.fullmatch(r'[0-9]{1,10}',a.exit):raise ValueError('Exit code format')
    d=decode(int(a.exit))
    if d['refused']:
        print('disc90_probe_refused path_construction=1 no_result=1');return
    for i,n in enumerate(BIT_NAMES):print('disc90_step name=%s ok=%d'%(n,d['bits'][i]))
    for n,_b,_s in CLASS_FIELDS:print('disc90_class step=%s class=%s'%(n,CLASS_NAMES[d['classes'][n]]))
    idx=d['s7_first_fail'];print('disc90_s7_first_fail index=%d name=%s'%(idx,IMPORTS[idx-1] if idx else 'none'))
    for r in readings(d):print('disc90_reading '+r)
    for rule in RULES:print('disc90_rule '+rule)
    print('disc90_limits no_python_code_run=1 no_Py_Initialize=1 diagnostic_only=1 negative_inconclusive=1 security_acceptance=NOT_ESTABLISHED')


if __name__=='__main__':
    try:main()
    except ValueError as exc:
        raw=sys.argv[sys.argv.index('--exit')+1] if '--exit' in sys.argv[:-1] else 'unknown'
        print('disc90_decode_refused raw_exit='+(raw if re.fullmatch(r'[0-9]{1,10}',raw) else 'unparsed')+' crash_or_out_of_range_report_raw_not_decoded=1 reason='+str(exc)[:60]);sys.exit(2)
