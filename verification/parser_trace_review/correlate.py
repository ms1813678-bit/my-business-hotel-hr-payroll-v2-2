"""Pure decoder for explicitly normalized ETW records. No recording or live I/O.
Adapter must preserve full timestamps, provider/version and loss counts. Refuses
ambiguity rather than assigning an operation to a name-matched process.
"""
FILE='edd08927-9cc4-4e65-b970-c2560fb5c289'
PROCESS='22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716'
class Refusal(ValueError):pass

def correlate(records,identity,events_lost):
    if type(events_lost)is not int or events_lost!=0:raise Refusal('event loss or unknown loss count')
    if len(records)>200000:raise Refusal('record count cap')
    required={'pid','ppid','created','image','launcher_started','launcher_ended'}
    if set(identity)!=required:raise Refusal('explicit full child identity required')
    pid=identity['pid'];created=identity['created']
    rows=sorted(records,key=lambda r:r['ts'])
    starts=[r for r in rows if r['provider']==PROCESS and r['id']==1 and r['data'].get('ProcessID')==pid]
    stops=[r for r in rows if r['provider']==PROCESS and r['id']==2 and r['data'].get('ProcessID')==pid]
    if len(starts)!=1 or len(stops)!=1:raise Refusal('unique process lifetime absent')
    start,stop=starts[0],stops[0];d=start['data']
    if d.get('CreateTime')!=created or stop['data'].get('CreateTime')!=created or d.get('ParentProcessID')!=identity['ppid'] or d.get('ImageName','').casefold()!=identity['image'].casefold():raise Refusal('process identity mismatch')
    if not identity['launcher_started']<=start['ts']<=stop['ts']<=identity['launcher_ended']:raise Refusal('child outside owner interval')
    threads={};lifetimes=[]
    for r in rows:
        if r['provider']!=PROCESS or r['id'] not in (3,4):continue
        d=r['data'];tid=d['ThreadID']
        if r['id']==3:
            if tid in threads:raise Refusal('overlapping TID lifetime')
            threads[tid]=(d['ProcessID'],r['ts'])
        else:
            before=threads.pop(tid,None)
            if before is None:
                if d['ProcessID']==pid:raise Refusal('owned thread stop without start')
                continue
            if before[0]!=d['ProcessID']:raise Refusal('thread process mismatch')
            if d['ProcessID']==pid:
                if not start['ts']<=before[1]<=r['ts']<=stop['ts']:raise Refusal('thread outside process lifetime')
                lifetimes.append((tid,before[1],r['ts']))
    if any(p==pid for p,_ in threads.values()) or not lifetimes:raise Refusal('owned thread lifetime incomplete')
    pending={};pairs=[]
    for r in rows:
        if r['provider']!=FILE:continue
        if r['id']==12:
            if r['version'] not in (0,1):raise Refusal('unsupported Create version')
            d=r['data'];irp=d['Irp'];tid=d['ThreadId'] if r['version']==0 else d['IssuingThreadId']
            owned=[x for x in lifetimes if x[0]==tid and x[1]<=r['ts']<=x[2]]
            if len(owned)>1:raise Refusal('ambiguous owned thread')
            # Track every Create, not just owned requests, to catch IRP reuse.
            if irp in pending:raise Refusal('overlapping IRP reuse')
            pending[irp]=(r,bool(owned))
        elif r['id']==24:
            if r['version']!=0:raise Refusal('unsupported OperationEnd version')
            before=pending.pop(r['data']['Irp'],None)
            if before and before[1]:
                op=before[0]
                if not op['ts']<=r['ts']<=stop['ts']:raise Refusal('operation outside lifetime')
                pairs.append(dict(ts=op['ts'],end_ts=r['ts'],path=op['data']['FileName'],status=r['data']['Status'],pid=pid))
    if any(owned for _,owned in pending.values()):raise Refusal('owned operation missing completion')
    if not pairs:raise Refusal('no owned correlated file operations')
    return dict(pid=pid,created=created,exit_code=stop['data']['ExitCode'],operations=pairs,notice='Rejected operations are not independently fatal loader causes. No network claim.')
