from datetime import date
from decimal import Decimal
import pytest
from app.leave_overlap_evidence import classify_record_overlap


def evidence():
    record=dict(id=1,employee_id=7,leave_type_id=3,start_date=date(2026,5,17),end_date=date(2026,5,18),calendar_days=2,charged_days=Decimal(2),status='confirmed')
    ledger=[dict(id=10,employee_id=7,leave_type_id=3,leave_record_id=1,transaction_type='leave_use',days_delta=Decimal(-2))]
    register=[dict(id=20+n,employee_id=7,leave_record_id=1,work_date=date(2026,5,17+n),code='A') for n in range(2)]
    return dict(record=record,ledger=ledger,register=register,basis=date(2026,5,17),through=date(2026,5,31),type_codes={3:'A'},tracked_types={3:True})


def test_exact_explicit_links_are_candidate_only_never_netting():
    result=classify_record_overlap(**evidence())
    assert result.state=='explicit_link_candidate_not_reconciled'
    assert not result.issues and not result.netting_allowed


@pytest.mark.parametrize('case',['fraction','partial','missing_day','foreign_employee','foreign_type','wrong_code','no_id_link','reversal','amount','draft'])
def test_link_conflicts_never_infer_equivalence(case):
    args=evidence()
    if case=='fraction':args['record']['charged_days']=Decimal('1.25')
    elif case=='partial':args['basis']=date(2026,5,18)
    elif case=='missing_day':args['register'].pop()
    elif case=='foreign_employee':args['ledger'][0]['employee_id']=8
    elif case=='foreign_type':args['ledger'][0]['leave_type_id']=4
    elif case=='wrong_code':args['register'][0]['code']='W'
    elif case=='no_id_link':args['register'][0]['leave_record_id']=None
    elif case=='reversal':args['ledger'].append(dict(args['ledger'][0],id=11,transaction_type='leave_reversal',days_delta=Decimal(2)))
    elif case=='amount':args['ledger'][0]['days_delta']=Decimal(-1)
    elif case=='draft':args['record']['status']='draft'
    result=classify_record_overlap(**args)
    assert result.issues and result.state=='unresolved_link_conflict' and not result.netting_allowed


def test_cancelled_pair_does_not_erase_still_linked_register():
    args=evidence();args['record']['status']='cancelled'
    args['ledger'].append(dict(args['ledger'][0],id=11,transaction_type='leave_reversal',days_delta=Decimal(2)))
    r=classify_record_overlap(**args)
    assert r.state=='cancelled_record_requires_review'
    assert 'cancelled_record_still_linked_in_register' in r.issues and not r.netting_allowed


def test_equal_dates_amounts_without_record_id_do_not_establish_link():
    args=evidence()
    for r in args['ledger']+args['register']:r['leave_record_id']=None
    r=classify_record_overlap(**args)
    assert r.ledger_ids==() and r.register_ids==() and r.issues
    assert not r.netting_allowed


def test_duplicate_use_rows_are_not_quantitative_proof():
    args=evidence();args['ledger'].append(dict(args['ledger'][0],id=11))
    assert 'linked_use_amount_or_count_disagrees' in classify_record_overlap(**args).issues



def test_nontracked_type_never_amount_candidate():
    args=evidence();args['tracked_types']={3:False}
    result=classify_record_overlap(**args)
    assert result.state=='unresolved_link_conflict' and 'record_type_not_balance_tracked' in result.issues



def test_duplicate_canonical_mapping_refuses_even_if_database_usually_prevents_it():
    args=evidence();args['type_codes'][4]='A'
    r=classify_record_overlap(**args)
    assert 'ambiguous_canonical_type_mapping' in r.issues and r.state=='unresolved_link_conflict'


@pytest.mark.parametrize('charge',[Decimal(0),Decimal(-1),Decimal('NaN')])
def test_invalid_charge_is_diagnostic_candidate_not_unrelated_abort(charge):
    args=evidence();args['record']['charged_days']=charge
    r=classify_record_overlap(**args)
    assert 'invalid_nonpositive_or_nonfinite_record_charge' in r.issues
    assert r.state=='unresolved_link_conflict' and not r.netting_allowed
