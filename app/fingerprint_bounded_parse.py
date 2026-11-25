"""CPU/memory/wall-bounded parse with strict, nonexecutable IPC.

Worker output is untrusted data. Both directions use bounded JSON, never pickle.
Timeout kills and waits. Error output is discarded, never accumulated in memory.
"""
import base64,json,os,subprocess,sys,tempfile,re
from datetime import date,datetime
from pathlib import Path
from hashlib import sha256
from types import MappingProxyType
from app.fingerprint_evidence import FingerprintEvidence
from app.attendance_import import normalize

OUTPUT_BYTES=4_000_000
PLATFORM_UNAVAILABLE_MESSAGE = 'قراءة ملف البصمة المعزولة غير متاحة على نظام التشغيل الحالي. لم يتم حفظ مراجعة أو تسجيل حضور. يلزم استكمال دعم ويندوز قبل المتابعة.'

class FingerprintPlatformUnavailable(ValueError):
    """No qualified isolation adapter exists for this platform."""

def _operational_platform():
    return os.name

def require_operational_parser_platform():
    if _operational_platform() != 'posix':
        raise FingerprintPlatformUnavailable(PLATFORM_UNAVAILABLE_MESSAGE)



def _decode_worker(raw,verified_aliases=None):
    if len(raw)>OUTPUT_BYTES:raise ValueError('Fingerprint parser result exceeds safety budget.')
    def pairs(items):
        out={}
        for k,v in items:
            if k in out:raise ValueError('Duplicate parser result key.')
            out[k]=v
        return out
    try:data=json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda _:(_ for _ in ()).throw(ValueError('Nonfinite result.')))
    except (ValueError,TypeError,RecursionError,UnicodeError) as exc:raise ValueError('Invalid parser result.') from exc
    fields={'status','period_start','period_end','occurrences','issues'}
    if type(data) is not dict or data.get('status')!='ok' or set(data)!=fields:raise ValueError('Original fingerprint source requires review.')
    def day(value):
        if value is None:return None
        if not isinstance(value,str) or re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}',value) is None:raise ValueError('Invalid parser date.')
        return date.fromisoformat(value)
    first,last=day(data['period_start']),day(data['period_end'])
    if (first is None)!=(last is None) or (first is not None and first>last):raise ValueError('Invalid parser period.')
    events=data['occurrences'];issues=data['issues']
    if type(events) is not list or len(events)>200000 or type(issues) is not list or len(issues)>20000:raise ValueError('Invalid parser result bounds.')
    event_fields={'employee_id','raw_employee_code','raw_employee_name','punch_at','raw_timestamp','event_type','source_sheet','source_row','event_index'}
    aliases={normalize(k):v for k,v in (verified_aliases or {}).items()}
    for event in events:
        if type(event) is not dict or set(event)!=event_fields:raise ValueError('Invalid parser occurrence schema.')
        if event['employee_id'] is not None and (type(event['employee_id']) is not int or not 1<=event['employee_id']<=2147483647):raise ValueError('Invalid parser identity.')
        if any(type(event[k]) is not int or not 0<=event[k]<=200000 for k in ('source_row','event_index')):raise ValueError('Invalid parser source position.')
        for k in ('raw_employee_code','raw_employee_name','raw_timestamp','event_type','source_sheet'):
            if event[k] is not None and (not isinstance(event[k],str) or len(event[k])>1000000):raise ValueError('Invalid parser text.')
        value=event['punch_at']
        if not isinstance(value,str) or len(value)>32:raise ValueError('Invalid parser timestamp.')
        try:stamp=datetime.fromisoformat(value)
        except ValueError as exc:raise ValueError('Invalid parser timestamp.') from exc
        if stamp.tzinfo is not None or stamp.isoformat()!=value:raise ValueError('Explicit naive parser timestamp required.')
        if first is None or not first<=stamp.date()<=last:raise ValueError('Occurrence outside parser source period.')
        if event['source_row']<1 or not isinstance(event['source_sheet'],str) or not event['source_sheet'] or not isinstance(event['event_type'],str) or not event['event_type'] or not isinstance(event['raw_timestamp'],str):raise ValueError('Exact source provenance required.')
        if verified_aliases is not None:
            # Raw mode binds code only, never a name-based guessed identity.
            expected=aliases.get(normalize(event['raw_employee_code'])) if event['raw_employee_code'] else None
            if event['employee_id']!=expected:raise ValueError('Worker identity differs from trusted alias inputs.')
        event['punch_at']=stamp
    allowed_issue_keys={'reason','sheet','row','employee_id','work_date','raw_employee_code','raw_employee_name','raw_time_value','raw_row','value'}
    for issue in issues:
        if type(issue) is not dict or not set(issue)<=allowed_issue_keys or not isinstance(issue.get('reason'),str):raise ValueError('Invalid parser issue schema.')
        if any(type(v) not in (str,int,float,type(None)) or (isinstance(v,str) and len(v)>1000000) for v in issue.values()):raise ValueError('Invalid parser issue values.')
        if 'work_date' in issue:issue['work_date']=day(issue['work_date'])
        if issue.get('employee_id') is not None and (type(issue['employee_id']) is not int or issue['employee_id']<1 or (verified_aliases is not None and issue['employee_id'] not in set(verified_aliases.values()))):raise ValueError('Invalid parser issue identity.')
        if issue.get('row') is not None and (type(issue['row']) is not int or issue['row']<0):raise ValueError('Invalid parser issue position.')
    try:
        for event in events:
            for value in event.values():
                if isinstance(value,str):value.encode('utf-8')
        for issue in issues:
            for value in issue.values():
                if isinstance(value,str):value.encode('utf-8')
    except UnicodeError as exc:raise ValueError('Valid UTF-8 parser result required.') from exc
    return first,last,events,issues


def parse_operational_fingerprint(payload,*,filename,verified_aliases):
    if not isinstance(payload,bytes) or not 1<=len(payload)<=10_000_000:raise ValueError('Bounded original fingerprint bytes required.')
    if not isinstance(filename,str) or len(filename)>1000 or type(verified_aliases) is not dict or len(verified_aliases)>100000:raise ValueError('Bounded explicit parser identity required.')
    if any(not isinstance(k,str) or not k or len(k)>1000 or type(v) is not int or not 1<=v<=2147483647 for k,v in verified_aliases.items()):raise ValueError('Exact parser aliases required.')
    if sum(len(k) for k in verified_aliases)>500000:raise ValueError('Aggregate alias input exceeds safety budget.')
    try:
        filename.encode('utf-8')
        for alias in verified_aliases:alias.encode('utf-8')
    except UnicodeError as exc:raise ValueError('Valid UTF-8 parser inputs required.') from exc
    encoded=json.dumps(dict(payload=base64.b64encode(payload).decode('ascii'),filename=filename,aliases=verified_aliases),ensure_ascii=False,separators=(',',':')).encode()
    if len(encoded)>16_000_000:raise ValueError('Bounded parser payload required.')
    require_operational_parser_platform()
    with tempfile.TemporaryFile() as output:
        try:
            result=subprocess.run([sys.executable,'-m','app.fingerprint_parse_worker'],input=encoded,
                stdout=output,stderr=subprocess.DEVNULL,timeout=3,check=False,
                env={'PATH':os.defpath,'PYTHONPATH':str(Path(__file__).resolve().parent.parent),'LANG':'C.UTF-8'},cwd=str(Path(__file__).resolve().parent.parent))
        except subprocess.TimeoutExpired as exc:raise ValueError('Fingerprint parser safety budget exceeded.') from exc
        if result.returncode!=0:raise ValueError('Fingerprint parser refused or exceeded safety budget.')
        if output.tell()>OUTPUT_BYTES:raise ValueError('Fingerprint parser result exceeds safety budget.')
        output.seek(0);first,last,events,issues=_decode_worker(output.read(OUTPUT_BYTES+1),verified_aliases)
    return FingerprintEvidence(sha256(payload).hexdigest(),payload,first,last,
        tuple(MappingProxyType(v) for v in events),tuple(MappingProxyType(v) for v in issues))
