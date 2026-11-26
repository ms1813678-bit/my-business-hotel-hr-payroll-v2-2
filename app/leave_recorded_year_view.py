"""Verified year-source view, separate net and metadata, never a balance or writer.

This adapter verifies the complete stored proof BEFORE extracting any field.
Metadata is never relabelled as the net, and v2 allocation row ids are retained.
"""
from fractions import Fraction
from app.leave_recorded_source_versions import verify_versioned_recorded_source


def read_verified_recorded_year_view(db, *, result, employee_id, leave_type_id):
    import json
    result = json.loads(json.dumps(result))
    verify_versioned_recorded_source(db, result=result, employee_id=employee_id, leave_type_id=leave_type_id)
    source = result['source']
    version = source.get('version')
    if 'version' not in source:
        completed = source
        allocations = ()
    elif version == 'year2-recorded-v1':
        completed = source['own']
        allocations = ()
    elif version == 'year2-recorded-v2':
        completed = source['base']['source']['own']
        allocations = source['allocations']
    else:
        raise ValueError('Unsupported recorded year view version.')
    rows = frozenset(row[0] for row in completed['source']['rows'])
    allocation_ids = frozenset(item['ledger_id'] for item in allocations)
    if rows & allocation_ids or len(allocation_ids) != len(allocations):
        raise ValueError('Recorded year view provenance overlaps.')
    return dict(full_net=Fraction(result['carried_amount']), held=Fraction(result['held_annual']),
                metadata_completed=completed, provenance_ids=rows | allocation_ids,
                allocation_ids=allocation_ids, posting_allowed=False)


def review_later_year_source(db, **selection):
    """Legacy baseline source stays byte-shape compatible, later years use v1.

    For pre-allocation closing choices only. Transition post-allocation reads
    must explicitly use the v2 builder instead; this is not a silent fallback.
    """
    from datetime import date
    from app.leave_opening_source import read_confirmed_opening_source
    from app.leave_completed_year_source import review_completed_year_source
    identity = {k: selection[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    reviews, _policy, _digest = read_confirmed_opening_source(db, selection_id=selection['selection_id'], **identity)
    years = {date.fromisoformat(row['effective_start']).year for row in reviews}
    if len(years) != 1:
        raise ValueError('One explicit opening baseline required.')
    if selection['source_year'] > years.pop():
        from app.leave_year2_recorded_source import build_year2_recorded_source
        built = build_year2_recorded_source(db, **selection)
        result = dict(built['stored'], held_annual=built['stored']['source']['held_annual'])
        view = read_verified_recorded_year_view(db, result=result,
            employee_id=selection['employee_id'], leave_type_id=selection['leave_type_id'])
        return dict(source=result['source'], source_digest=result['source_digest'],
                    amount=view['full_net'], held_annual=view['held'], view=view)
    source = review_completed_year_source(db, **selection)
    # Baseline live review preserves its existing source verifier and shape.
    # It can include a zero row during a later exclusivity refusal; that live
    # source is not a newly recorded carry grant, so do not force legacy
    # recorded-grant verification before the caller's unchanged exclusivity gate.
    metadata = source['source']
    view = dict(full_net=Fraction(source['amount']), held=Fraction(source['held_annual']),
                metadata_completed=metadata,
                provenance_ids=frozenset(row[0] for row in metadata['source']['rows']),
                allocation_ids=frozenset(), posting_allowed=False)
    return dict(source, view=view)


def review_transition_year_source(db, **selection):
    """Completed post-closing source. Baseline keeps the existing live shape.

    Later years explicitly use v2 so their own verified cash/zero exclusions
    are included once. This function is ONLY for a year-transition checkpoint.
    """
    from datetime import date
    from app.leave_opening_source import read_confirmed_opening_source
    identity = {k: selection[k] for k in ('actor_id', 'session_id', 'session_version', 'expected_epoch')}
    reviews, _policy, _digest = read_confirmed_opening_source(db, selection_id=selection['selection_id'], **identity)
    years = {date.fromisoformat(row['effective_start']).year for row in reviews}
    if len(years) != 1:
        raise ValueError('One explicit opening baseline required.')
    if selection['source_year'] <= years.pop():
        return review_later_year_source(db, **selection)
    from app.leave_year2_recorded_source_v2 import build_year2_recorded_source_v2
    built = build_year2_recorded_source_v2(db, **selection)
    result = dict(built['stored'], held_annual=built['stored']['source']['held_annual'])
    view = read_verified_recorded_year_view(db, result=result,
        employee_id=selection['employee_id'], leave_type_id=selection['leave_type_id'])
    return dict(source=result['source'], source_digest=result['source_digest'],
                amount=view['full_net'], held_annual=view['held'], view=view)
