"""Review-only child identity binding and narrow output policy.
No baseline mutation. Uses trusted launcher PID+stderr numeric child ID, exact
staged executable mapping and owner interval, never basename matching.
"""
import re,hashlib,hmac,ntpath
from correlate import PROCESS,Refusal

def bind(records,launcher_pid,child_pid,exact_image,started,ended):
    if type(launcher_pid)is not int or type(child_pid)is not int or launcher_pid<=0 or child_pid<=0 or launcher_pid==child_pid:raise Refusal('trusted numeric launcher/child required')
    if not isinstance(exact_image,str) or not ntpath.isabs(exact_image) or started>=ended:raise Refusal('full expected image and owner interval required')
    candidates=[r for r in records if r['provider']==PROCESS and r['id']==1 and r['data'].get('ProcessID')==child_pid]
    if len(candidates)!=1:raise Refusal('unique exact child start missing')
    r=candidates[0];d=r['data']
    if d.get('ParentProcessID')!=launcher_pid or d.get('ImageName','').casefold()!=exact_image.casefold() or not started<=r['ts']<=ended:raise Refusal('child parent/image/interval mismatch')
    created=d.get('CreateTime')
    if type(created)is not int or created>r['ts'] or created<started:raise Refusal('exact child creation time outside owner interval')
    return dict(pid=child_pid,ppid=launcher_pid,created=created,image=exact_image,launcher_started=started,launcher_ended=ended)

def child_id(diagnostic):
    if type(diagnostic)is not bytes or len(diagnostic)>16384:raise Refusal('bounded native diagnostic required')
    ids=re.findall(rb'^CANDIDATE_CREATED pid=([0-9]{1,10}) suspended=1\r?$',diagnostic,re.M)
    if len(ids)!=1:raise Refusal('single trusted native child identity missing')
    n=int(ids[0])
    if not 0<n<=0xffffffff:raise Refusal('PID range')
    return n

def sanitized(result,key,allowed_basenames):
    if type(key)is not bytes or len(key)!=32:raise Refusal('fresh 256-bit local HMAC key required')
    if not isinstance(allowed_basenames,set) or len(allowed_basenames)>32 or any(not isinstance(n,str) or ntpath.basename(n)!=n for n in allowed_basenames):raise Refusal('explicit synthetic basename allowlist')
    if len(result['operations'])>20000:raise Refusal('output operation cap')
    output=[]
    for op in result['operations']:
        path=op['path'];base=ntpath.basename(path)
        if type(path)is not str or not path or len(path)>32768:raise Refusal('bounded decoded path required')
        status=op['status']
        if type(status)is not int or not 0<=status<=0xffffffff:raise Refusal('status range')
        item=dict(status=status,path_hmac=hmac.new(key,path.encode('utf-16-le'),hashlib.sha256).hexdigest())
        if base in allowed_basenames:item['synthetic_basename']=base
        output.append(item)
    return dict(exit_code=result['exit_code'],operations=output,operation_count=len(output),cause_established=False,network_acceptance=False)
