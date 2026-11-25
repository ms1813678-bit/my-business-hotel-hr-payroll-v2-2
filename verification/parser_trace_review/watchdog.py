"""Review-only watchdog. Fixed owned paths independent of mutable journal.
No recording start path. Each stop/query and raw cleanup phase is independent.
"""
import json,subprocess,time,sys,pathlib,os,ctypes
NAMES=('HOnYParserFile','HOnYParserProcess')
RAW=('file.etl','process.etl','events.xml','summary.txt','convert-output')
def alive_handle(pid):
    k=ctypes.WinDLL('kernel32',use_last_error=True)
    k.OpenProcess.argtypes=[ctypes.c_uint32,ctypes.c_int,ctypes.c_uint32];k.OpenProcess.restype=ctypes.c_void_p
    k.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_uint32];k.WaitForSingleObject.restype=ctypes.c_uint32
    k.CloseHandle.argtypes=[ctypes.c_void_p];k.CloseHandle.restype=ctypes.c_int
    h=k.OpenProcess(0x100000,False,pid)
    if not h:raise ValueError('owned parent synchronize handle required before readiness')
    return k,h

def stop_all(probe,root,invoke=subprocess.run):
    out={}
    for name,basename in zip(NAMES,RAW):
        item=dict(absent=False,stop_return=None)
        try:
            p=invoke([probe,'stop-path',name,str(root/basename)],capture_output=True,timeout=3)
            item['stop_return']=p.returncode
        except BaseException:item['stop_error']=True
        # Even timeout of stop never skips query or other session.
        try:
            q=invoke([probe,'query',name],capture_output=True,timeout=3)
            item['absent']=q.returncode==0 and json.loads(q.stdout).get('absent') is True
        except BaseException:item['query_error']=True
        out[name]=item
    return out

def remove_raw(root):
    errors=[]
    for name in RAW:
        p=root/name
        try:
            if p.is_symlink() or (p.exists() and not p.is_file()):raise ValueError('unexpected raw entry type')
            p.unlink(missing_ok=True)
            if p.exists():raise ValueError('raw absence not confirmed')
        except BaseException:errors.append(name)
    return not errors

def run(probe,root,pid):
    root=pathlib.Path(root);pid=int(pid)
    if root.is_symlink() or not root.is_dir() or pid<=0:raise ValueError('exact owned root and parent required')
    k,h=alive_handle(pid);out={};error=False;dead=False;started=time.monotonic()
    try:
        (root/'watchdog-ready').write_text(str(os.getpid()))
        try:
            while time.monotonic()-started<10:
                state=k.WaitForSingleObject(h,0)
                if state not in (0,258):raise ValueError('owner wait failed')
                dead=state==0
                if dead or (root/'stop-now').exists():break
                time.sleep(.05)
        except BaseException:error=True
        finally:out=stop_all(probe,root)
        absent=set(out)==set(NAMES) and all(x['absent'] for x in out.values())
        # Keep crash cleanup alive through bounded conversion. Parent waits only
        # for session-stop marker, not this process exit, before converting.
        (root/'watchdog-stopped.json').write_text(json.dumps(dict(sessions=out,complete=absent and not error)))
        until=time.monotonic()+20
        while not dead and not (root/'conversion-done').exists() and time.monotonic()<until:
            state=k.WaitForSingleObject(h,0)
            if state not in (0,258):error=True;break
            dead=state==0;time.sleep(.05)
        # After normal completion or parent crash, raw files must be absent.
        cleaned=remove_raw(root) if absent else False
        (root/'watchdog-result.json').write_text(json.dumps(dict(sessions=out,complete=absent and cleaned and not error,raw_absent=cleaned,owner_dead=dead)))
    finally:k.CloseHandle(h)
if __name__=='__main__':run(*sys.argv[1:])
