"""Pure first-install compensation planner. No service/files/registry mutations.
Does not authorize rollback. Native executor must revalidate identity under held
handles and make each compensation independently durable before/after effects.
"""
from dataclasses import dataclass
import json,re

class Refusal(ValueError):pass
ACTIONS={'release','service','shortcut','certificate','firewall'}
@dataclass(frozen=True)
class Owned:
    kind:str
    identity:str
    before_absent:bool
    after_fingerprint:str

@dataclass(frozen=True)
class Undo:
    index:int
    kind:str
    identity:str
    expected_fingerprint:str

def plan(records,live):
    if type(records)is not list or len(records)>256 or type(live)is not dict:raise Refusal('bounded explicit ownership snapshot required')
    seen=set();steps=[];preserve=[]
    for i,r in enumerate(records):
        if not isinstance(r,Owned) or r.kind not in ACTIONS or not isinstance(r.identity,str) or not 1<=len(r.identity)<=1024 or '\0' in r.identity:raise Refusal('invalid ownership record')
        if type(r.before_absent)is not bool or not re.fullmatch('[0-9a-f]{64}',r.after_fingerprint):raise Refusal('explicit absence and digest required')
        key=(r.kind,r.identity)
        if key in seen:raise Refusal('duplicate transaction identity')
        seen.add(key)
        current=live.get(key)
        if not r.before_absent:preserve.append((i,'preexisting'));continue
        if current is None:preserve.append((i,'already_absent'));continue
        if current!=r.after_fingerprint:preserve.append((i,'identity_changed'));continue
        steps.append(Undo(i,r.kind,r.identity,r.after_fingerprint))
    # Reverse mutation order. Service creation must be after release creation in
    # a valid install journal, so its compensation precedes release removal.
    indices={r.kind:i for i,r in enumerate(records)}
    if 'service' in indices and 'release' in indices and indices['service']<indices['release']:raise Refusal('service preceded release ownership')
    return list(reversed(steps)),preserve

def compensate(steps,executor):
    """Each executor step owns final live identity recheck. Never short-circuit."""
    results=[];service_safe=True
    for step in steps:
        if step.kind=='release' and not service_safe:
            results.append((step.index,'preserved_service_unconfirmed'));continue
        try:
            outcome=executor(step)
            if outcome not in ('compensated','already_absent','preserved_identity_changed'):raise Refusal('unverified executor outcome')
            results.append((step.index,outcome))
            if step.kind=='service' and outcome not in ('compensated','already_absent'):service_safe=False
        except BaseException:
            results.append((step.index,'failed_unconfirmed'))
            if step.kind=='service':service_safe=False
    return dict(results=results,complete=all(s in ('compensated','already_absent') for _,s in results))

def decode_journal(raw):
    if type(raw)is not bytes or not 1<=len(raw)<=262144:raise Refusal('journal cap')
    def unique(pairs):
        d={}
        for k,v in pairs:
            if k in d:raise Refusal('duplicate journal key')
            d[k]=v
        return d
    try:d=json.loads(raw,object_pairs_hook=unique)
    except (ValueError,UnicodeError) as e:raise Refusal('invalid journal') from e
    if type(d)is not dict or set(d)!={'version','transaction','phase','records'} or d['version']!=1 or type(d['version'])is not int:raise Refusal('journal schema')
    if not isinstance(d['transaction'],str) or not re.fullmatch('[0-9a-f]{32}',d['transaction']) or d['phase'] not in ('staged','committing','compensating','finished'):raise Refusal('journal owner/state')
    if type(d['records'])is not list or len(d['records'])>256:raise Refusal('record cap')
    records=[]
    for r in d['records']:
        if type(r)is not dict or set(r)!={'kind','identity','before_absent','after_fingerprint'}:raise Refusal('record schema')
        records.append(Owned(**r))
    plan(records,{}) # validate shape without asserting current external state
    return d['transaction'],d['phase'],records
