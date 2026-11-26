"""Decode the forwarder-probe exit code. Parsed from the existing capture line only."""
import argparse
import re
import sys

RULES = [
 'p1_some_target_fails=that_file_is_named;forwarder_hypothesis_supported;a_fix_is_a_separate_design_needing_owner_consent',
 'all_p1_load_and_p2_and_p3_succeed=loader_search_path_dependent;a_fix_needs_a_new_design_and_owner_consent',
 'all_p1_load_and_p0_p2_p3_all_fail=forwarders_and_search_path_excluded;remaining_TLS_callback_DllMain_manifest_untestable_without_a_trace_tool;instrument_exhausted',
 'p0_succeeds=baseline_did_not_reproduce;stop',
 'any_other_combination=report_raw_only;no_cause_inferred',
 'raw_exit_code_outside_probe_range=crash_or_exception;report_raw_do_not_decode',
 'no_further_instrument_after_this_run;next_step_is_analysis_only',
]


def load_names(path):
    names = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            m = re.fullmatch(r'target index=([0-9]+) name=([a-z0-9_.-]+\.dll) forwarded_by=\S*\n?', line)
            if m:
                if int(m.group(1)) != len(names) + 1:
                    raise ValueError('Target listing out of order')
                names.append(m.group(2))
    if not 1 <= len(names) <= 20:
        raise ValueError('Target listing count out of range')
    return names


def decode(code, count):
    if type(code) is not int or type(count) is not int or not 1 <= count <= 20:
        raise ValueError('Bad arguments')
    if not 0x20000000 <= code <= 0x3FFFFFFF or code >> 29 != 1:
        raise ValueError('Not a forwarder probe exit code')
    if code >> 28 & 1:
        raise ValueError('Spare bit set')
    mask = code & 0x7FFFFF
    first = code >> 23 & 0x1F
    p1 = mask & 0xFFFFF
    if p1 >> count:
        raise ValueError('Target bit beyond count')
    expected = 0
    for k in range(count):
        if not p1 >> k & 1:
            expected = k + 1
            break
    if first != expected:
        raise ValueError('First-failing field inconsistent with mask')
    return {'p0': mask >> 22 & 1, 'p1': [p1 >> k & 1 for k in range(count)], 'p2': mask >> 20 & 1, 'p3': mask >> 21 & 1, 'first': first}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--exit', required=True)
    p.add_argument('--targets-file', required=True)
    a = p.parse_args()
    if not re.fullmatch(r'[0-9]{1,10}', a.exit):
        raise ValueError('Exit code format')
    names = load_names(a.targets_file)
    r = decode(int(a.exit), len(names))
    print('forwarder_p0 baseline_python314_loaded=%d expected=0' % r['p0'])
    for i, n in enumerate(names):
        print('forwarder_p1 index=%d name=%s loaded=%d' % (i + 1, n, r['p1'][i]))
    print('forwarder_p2 search_default_dirs_plus_dll_load_dir_loaded=%d' % r['p2'])
    print('forwarder_p3 altered_search_path_loaded=%d' % r['p3'])
    print('forwarder_first_fail_target index=%d name=%s' % (r['first'], names[r['first'] - 1] if r['first'] else 'none'))
    all_p1 = all(r['p1'])
    if not all_p1:
        reading = 'p1_some_target_fails'
    elif r['p0']:
        reading = 'p0_succeeds'
    elif r['p2'] and r['p3']:
        reading = 'all_p1_load_and_p2_and_p3_succeed'
    elif not r['p2'] and not r['p3']:
        reading = 'all_p1_load_and_p0_p2_p3_all_fail'
    else:
        reading = 'any_other_combination'
    print('forwarder_reading ' + reading)
    for rule in RULES:
        print('forwarder_rule ' + rule)
    print('forwarder_limits no_python_code_run=1 no_error_code_recorded=1 diagnostic_only=1 negative_inconclusive=1 security_acceptance=NOT_ESTABLISHED no_cause_claimed_beyond_a_named_file=1')


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as exc:
        raw = sys.argv[sys.argv.index('--exit') + 1] if '--exit' in sys.argv[:-1] else 'unknown'
        print('forwarder_decode_refused raw_exit=' + (raw if re.fullmatch(r'[0-9]{1,10}', raw) else 'unparsed') + ' crash_or_out_of_range_report_raw_not_decoded=1 reason=' + str(exc)[:60])
        sys.exit(2)
