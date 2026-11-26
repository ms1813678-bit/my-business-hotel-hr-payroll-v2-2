"""Review-only report over raw fingerprint punches: joined HH:MM cells and exact duplicate punches.

Technical settlement under Main's delegation (not an owner rule): raw punches are always preserved, duplicates are
SHOWN for HR review and never dropped, and nothing here decides attendance, a shift, or a payroll/leave effect.

The fingerprint terminal writes several punches of one day into one cell with no separator ("10:2816:31").
Fixed-width HH:MM tokens split that text in exactly one way, so such a cell is `joined_exact`. A cell is NOT exact
when it holds seconds (HH:MM:SS versus joined HH:MM is ambiguous), a leftover fragment, or any unreadable text; it
is then reported as `unreadable` with the leftover, and HR must read it. Only identical times inside one cell are
called duplicates; no time-distance threshold is invented. Input is the result of parse_attendance_workbook with
preserve_duplicate_punches=True; this module never reads files and never writes."""
import re
from collections import Counter, defaultdict
from datetime import date, time

HHMM = re.compile(r'(?:[01]\d|2[0-3]):[0-5]\d')
SEPARATORS = ' \t\r\n,;|'


def split_joined_hhmm(raw):
    """Greedy fixed-width HH:MM split. Returns {'tokens', 'exact', 'remainder'}; never guesses at a leftover."""
    if not isinstance(raw, str):
        raise ValueError('Cell text must be a string.')
    text = raw.strip().translate(str.maketrans('٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹', '01234567890123456789'))
    tokens, position = [], 0
    while position < len(text):
        if text[position] in SEPARATORS:
            position += 1
            continue
        match = HHMM.match(text, position)
        if match is None or text[match.end():match.end() + 1] == ':':     # unreadable text, or seconds follow
            return {'tokens': tokens, 'exact': False, 'remainder': text[position:]}
        tokens.append(time.fromisoformat(match.group()))
        position = match.end()
    return {'tokens': tokens, 'exact': True, 'remainder': ''}


def build_punch_review(parsed):
    """Per employee-day report. Every parsed occurrence appears exactly once; none is removed."""
    if not isinstance(parsed, dict) or not isinstance(parsed.get('events'), list) or not isinstance(parsed.get('issues'), list):
        raise ValueError('Parsed fingerprint evidence with events and issues required.')
    cells = {}
    occurrences = defaultdict(list)
    unmatched = 0
    for event in parsed['events']:
        employee_id, stamp = event.get('employee_id'), event.get('punch_at')
        if employee_id is None:
            unmatched += 1
            continue
        key = (employee_id, stamp.date(), event['source_sheet'], event['source_row'])
        occurrences[key].append(event)
        cells[key] = event['raw_timestamp']
    for issue in parsed['issues']:                       # a cell with no readable token yields no event
        if issue.get('reason') == 'invalid_time_value' and issue.get('employee_id') is not None and isinstance(issue.get('work_date'), date):
            key = (issue['employee_id'], issue['work_date'], issue['sheet'], issue['row'])
            cells.setdefault(key, issue['value'])
    days = []
    for key in sorted(cells, key=lambda k: (k[0], k[1], k[2], k[3])):
        employee_id, work_date, sheet, row = key
        events = sorted(occurrences.get(key, []), key=lambda e: e['event_index'])
        split = split_joined_hhmm(cells[key])
        times = [e['punch_at'].time().replace(microsecond=0) for e in events]
        duplicates = [{'time': t.strftime('%H:%M:%S'), 'count': n} for t, n in sorted(Counter(times).items()) if n > 1]
        if not split['exact']:
            state = 'unreadable_needs_hr_review'
        elif duplicates:
            state = 'duplicate_needs_hr_review'
        else:
            state = 'joined_exact_preserved' if len(split['tokens']) > 1 else 'single_punch_preserved'
        days.append({'employee_id': employee_id, 'work_date': work_date.isoformat(), 'sheet': sheet, 'row': row,
                     'raw_cell': cells[key], 'occurrences': [e['punch_at'].isoformat() for e in events],
                     'joined_exact': split['exact'] and len(split['tokens']) > 1, 'duplicates': duplicates,
                     'unreadable_remainder': split['remainder'], 'review_state': state})
    total = sum(len(d['occurrences']) for d in days)
    if total + unmatched != len(parsed['events']):
        raise ValueError('Punch review lost or invented an occurrence.')
    states = Counter(d['review_state'] for d in days)
    return {'days': days, 'summary': {'employee_days': len(days), 'occurrences': len(parsed['events']),
            'unmatched_identity_occurrences': unmatched, 'states': dict(sorted(states.items())),
            'other_source_issues': dict(sorted(Counter(i.get('reason') for i in parsed['issues'] if i.get('reason') not in ('invalid_time_value', 'duplicate_punch')).items())),
            'duplicate_extra_occurrences': sum(c['count'] - 1 for d in days for c in d['duplicates'])},
            'dropped_occurrences': 0, 'attendance_decided': False, 'registration_allowed': False}
