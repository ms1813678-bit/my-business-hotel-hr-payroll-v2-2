"""Prototype bounded first-install intent log. Not product installer wiring.
Exclusive creation, write-through flush via fsync, sequence/hash chain and strict
transition reader. Native owner-held directory binding is still required before
use outside a disposable test. Journal contents are never secret-bearing.
"""
import hashlib,json,os
from pathlib import Path
class Refusal(ValueError):pass
CAP=262144
KINDS={'release','service','shortcut','certificate','firewall'}
PHASES={'intent','verified','compensate_intent','compensated','preserved','failed'}
def canonical(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode('ascii')
def unique(pairs):
    out={}
    for k,v in pairs:
        if k in out:raise Refusal('duplicate log key')
        out[k]=v
    return out

def validate_action(a):
    if type(a)is not dict or set(a)!={'kind','identity_digest','before_absent','after_digest'}:raise Refusal('action schema')
    if a['kind'] not in KINDS or type(a['before_absent'])is not bool:raise Refusal('action kind/absence')
    for k in ('identity_digest','after_digest'):
        if type(a[k])is not str or len(a[k])!=64 or any(c not in '0123456789abcdef' for c in a[k]):raise Refusal('action digest')

def advance(states,record):
    if type(record)is not dict or set(record)!={'index','phase','action'} or type(record['index'])is not int or not 0<=record['index']<256 or record['phase'] not in PHASES:raise Refusal('log record schema')
    i=record['index'];phase=record['phase'];a=record['action'];validate_action(a)
    old=states.get(i)
    allowed={None:{'intent'},'intent':{'verified','compensate_intent','failed','preserved'},'verified':{'compensate_intent'},'compensate_intent':{'compensated','preserved','failed'},'failed':{'compensate_intent'},'preserved':set(),'compensated':set()}
    if phase not in allowed[old[0] if old else None] or (old and old[1]!=a):raise Refusal('invalid phase or changed action')
    states[i]=(phase,a)

class Journal:
    def __init__(self,path):
        self.path=Path(path);self.fd=None;self.states={};self.seq=0;self.previous='0'*64;self.size=0
        # No append/recovery mutation path. A read-only recovery plan uses read().
        self.fd=os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_BINARY',0),0o600)
    def append(self,index,phase,action):
        if self.fd is None:raise Refusal('closed journal')
        record=dict(index=index,phase=phase,action=action);states=self.states.copy();advance(states,record)
        payload=dict(seq=self.seq,previous=self.previous,record=record)
        digest=hashlib.sha256(canonical(payload)).hexdigest();line=canonical(dict(payload=payload,digest=digest))+b'\n'
        if len(line)>4096 or self.size+len(line)>CAP:raise Refusal('journal cap')
        try:
            view=memoryview(line)
            while view:
                written=os.write(self.fd,view)
                if written<=0:raise OSError('journal short write')
                view=view[written:]
            os.fsync(self.fd)
        except BaseException:
            self.close();raise
        self.states=states;self.seq+=1;self.previous=digest;self.size+=len(line)
    def close(self):
        if self.fd is not None:
            fd=self.fd;self.fd=None;os.close(fd)
    def __enter__(self):return self
    def __exit__(self,*args):self.close()

def read(path):
    with Path(path).open('rb') as f:raw=f.read(CAP+1)
    if not raw or len(raw)>CAP or not raw.endswith(b'\n'):raise Refusal('empty/oversize/torn log')
    states={};previous='0'*64;seq=0
    for line in raw.splitlines():
        if not line or len(line)>4096:raise Refusal('line cap')
        try:value=json.loads(line,object_pairs_hook=unique)
        except (ValueError,UnicodeError) as e:raise Refusal('malformed log') from e
        if type(value)is not dict or set(value)!={'payload','digest'}:raise Refusal('envelope schema')
        p=value['payload']
        if type(p)is not dict or set(p)!={'seq','previous','record'} or type(p['seq'])is not int or p['seq']!=seq or p['previous']!=previous:raise Refusal('sequence mismatch')
        digest=hashlib.sha256(canonical(p)).hexdigest()
        if value['digest']!=digest:raise Refusal('digest mismatch')
        advance(states,p['record']);previous=digest;seq+=1
    # Integrity against accidental damage/reordering only. Not authentication.
    # A replaced complete log can be rehashed, native owner identity is required.
    return dict(states=states,records=seq,tip=previous)
