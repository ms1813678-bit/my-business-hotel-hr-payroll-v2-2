"""Isolated adapter: verified year-2 net source -> the exact completed-year return shape.

Not wired anywhere. The completed-year shape stored by consumers is re-verified later
(leave_carry_availability._verify_recorded_source) with exact key sets and the rule
inner amount == carried amount == sum of the selected year-tagged rows (+ year attributions).
A year-2 net that adds back usage attributed to an earlier grant is NOT expressible in that
shape without losing the attribution identity, so the adapter refuses it (fail closed)
until the verifier carries that identity. No ledger write, no policy.
"""
from app.leave_exact_amount import as_fraction
from app.leave_month_earning import digest
from app.leave_year2_net_source import review_year2_net_source

COMPLETED_KEYS = ('source', 'selection_id', 'opening_digest', 'opening_apply', 'covered_months',
                  'verified_months', 'live_final_source', 'current_tiers', 'reviewed_on')


def review_year2_as_completed_year_source(db, **selection):
    net = review_year2_net_source(db, **selection)
    parts = net['completed_parts']
    if as_fraction(net['attributed']) != 0:
        raise ValueError('Year-2 net with usage attributed to an earlier grant needs a verifier that records that attribution.')
    if as_fraction(net['amount']) != as_fraction(net['own_rows_amount']):
        raise ValueError('Year-2 net must equal its recorded year-tagged rows in the completed-year shape.')
    payload = dict(source=parts['carry_payload'], selection_id=net['source']['selection_id'],
        opening_digest=parts['opening_digest'], opening_apply=parts['opening_apply'],
        covered_months=parts['covered_months'], verified_months=parts['verified_months'],
        live_final_source=parts['live_final_source'], current_tiers=parts['current_tiers'],
        reviewed_on=selection['reviewed_on'].isoformat())
    if set(payload) != set(COMPLETED_KEYS):
        raise ValueError('Exact completed-year shape required.')
    return dict(source=payload, source_digest=digest(payload), amount=net['amount'], held_annual=parts['held_annual'],
        source_verified=True, posting_allowed=False, unresolved=(), year2_net_source_digest=net['source_digest'])
