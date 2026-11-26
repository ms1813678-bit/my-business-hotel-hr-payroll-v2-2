"""Explicit recorded-source version routing. No live earning admission or posting.

Only consumers explicitly moved to this helper use the new version. The legacy
verifier remains unchanged. Unknown version tags never fall back to legacy.
"""

def verify_versioned_recorded_source(db, *, result, employee_id, leave_type_id):
    from app.leave_carry_availability import _verify_recorded_source

    if not isinstance(result, dict) or not {'source', 'source_digest', 'source_year', 'carried_amount', 'held_annual'} <= set(result):
        raise ValueError('Complete recorded source binding required.')
    source = result.get('source')
    if not isinstance(source, dict):
        raise ValueError('Recorded source object required.')
    if 'version' not in source:
        return _verify_recorded_source(db, result=result, employee_id=employee_id,
                                       leave_type_id=leave_type_id)
    from app.leave_year2_recorded_source import VERSION, verify_year2_recorded_source, _amount

    if source['version'] != VERSION:
        raise ValueError('Unsupported recorded source version.')
    if (type(result.get('source_year')) is not int
            or type(source.get('source_year')) is not int
            or source['source_year'] != result['source_year']
            or type(source.get('employee_id')) is not int
            or type(source.get('leave_type_id')) is not int
            or source['employee_id'] != employee_id
            or source['leave_type_id'] != leave_type_id):
        raise ValueError('Versioned recorded source identity changed.')
    held = _amount(result.get('held_annual'), 'caller held annual')
    if held != _amount(source.get('held_annual'), 'source held annual'):
        raise ValueError('Versioned recorded held amount changed.')
    verify_year2_recorded_source(db, stored={k: result[k] for k in
        ('source', 'source_digest', 'source_year', 'carried_amount')},
        employee_id=employee_id, leave_type_id=leave_type_id)
    # Return named source facts for existing type checks, never relabel the own
    # rows as the net. The proof's own amount is independently verified above.
    inner = dict(source['own']['source'])
    return inner
