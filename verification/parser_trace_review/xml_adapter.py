"""Review-only strict tracerpt XML adapter. All raw paths remain local."""
from pathlib import Path
import re,datetime,xml.etree.ElementTree as ET
from correlate import FILE,PROCESS,Refusal

def stamp(s):
    m=re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z',s)
    if not m:raise Refusal('exact UTC timestamp unsupported')
    t=datetime.datetime.strptime(m[1],'%Y-%m-%dT%H:%M:%S').replace(tzinfo=datetime.timezone.utc)
    return int(t.timestamp())*1000000000+int((m[2] or '').ljust(9,'0'))
def number(s):
    if not re.fullmatch(r'(?:0[xX][0-9a-fA-F]{1,16}|[0-9]{1,20})',s):raise Refusal('invalid integer')
    n=int(s,16 if s.lower().startswith('0x') else 10)
    if n>0xffffffffffffffff:raise Refusal('integer overflow')
    return n

def load(path):
    path=Path(path)
    if not 0<path.stat().st_size<=268435456:raise Refusal('XML byte cap')
    # Reject declarations before XML parser has any opportunity to resolve them.
    with path.open('rb') as f:
        tail=b''
        while block:=f.read(65536):
            text=(tail+block).replace(b'\0',b'').upper()
            if b'<!DOCTYPE' in text or b'<!ENTITY' in text:raise Refusal('XML declarations forbidden')
            tail=block[-128:]
    rows=[];count=0;root=None
    for event,el in ET.iterparse(path,events=('start','end')):
        if root is None:root=el
        if event!='end':continue
        if el.tag.rsplit('}',1)[-1]!='Event':continue
        count+=1
        if count>200000:raise Refusal('event count cap')
        prefix=el.tag[:-5]
        if prefix not in ('{http://schemas.microsoft.com/win/2004/08/events/event}','{https://schemas.microsoft.com/win/2004/08/events/event}'):raise Refusal('unsupported event namespace')
        def node(name):
            e=el.find('/'.join(prefix+n for n in name.split('/')))
            if e is None:raise Refusal('missing XML field '+name)
            return e
        provider=node('System/Provider').get('Guid','').strip('{}').lower()
        if provider not in (FILE,PROCESS):el.clear();root.clear();continue
        id=number(node('System/EventID').text or '')
        if (provider==FILE and id not in (12,24)) or (provider==PROCESS and id not in (1,2,3,4)):el.clear();root.clear();continue
        version=number(node('System/Version').text or '')
        valid=(provider==FILE and ((id==12 and version in (0,1)) or (id==24 and version==0))) or (provider==PROCESS and ((id==1 and version<=3) or (id==2 and version<=2) or (id in (3,4) and version<=1)))
        if not valid:raise Refusal('unverified provider event version')
        ts=stamp(node('System/TimeCreated').get('SystemTime',''))
        raw={}
        for e in node('EventData'):
            name=e.get('Name')
            if not name or name in raw or len(e.text or '')>32768:raise Refusal('missing/duplicate/oversize payload')
            raw[name]=e.text or ''
        keys=({'Irp','FileName','ThreadId' if version==0 else 'IssuingThreadId'} if id==12 else {'Irp','Status'}) if provider==FILE else ({1:{'ProcessID','ParentProcessID','CreateTime','ImageName'},2:{'ProcessID','CreateTime','ExitCode'},3:{'ProcessID','ThreadID'},4:{'ProcessID','ThreadID'}}[id])
        if not keys<=raw.keys():raise Refusal('payload schema mismatch')
        data={}
        for key in keys:
            data[key]=stamp(raw[key]) if key=='CreateTime' else raw[key] if key in ('ImageName','FileName') else number(raw[key])
        rows.append(dict(provider=provider,id=id,version=version,ts=ts,data=data));el.clear();root.clear()
    return rows
