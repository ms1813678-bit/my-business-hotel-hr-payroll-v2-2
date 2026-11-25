"""Review-only independent session watchdog. No recording start path.
Only accepts a prearmed journal with exact owned ETL paths, before any start.
Fixed descriptive session names, exact fresh-root ETL path ownership. Never shared sessions.
"""
import json,subprocess,time,sys,pathlib,os
NAMES={'HOnYParserFile','HOnYParserProcess'}
def read_journal(path,root):
    path=pathlib.Path(path);root=pathlib.Path(root)
    if path.parent!=root or path.is_symlink() or path.stat().st_size>16384:raise ValueError('owned bounded journal required')
    data=json.loads(path.read_text())
    if set(data)!={'deadline','sessions','owner_pid'} or set(data['sessions'])-NAMES or len(data['sessions'])>2:raise ValueError('invalid journal')
    if any(not isinstance(h,str) or pathlib.Path(h).parent!=root or pathlib.Path(h).name not in ('file.etl','process.etl') for h in data['sessions'].values()):raise ValueError('invalid trace paths')
    return data

def run(probe,journal,root):
    # Parent makes ready marker only after watchdog has announced startup.
    root=pathlib.Path(root);(root/'watchdog-ready').write_text(str(os.getpid()))
    started=time.monotonic();out={};last={}
    try:
        while time.monotonic()-started<15:
            if pathlib.Path(journal).exists():
                data=read_journal(journal,root)
                last.update(data['sessions'])
                if time.monotonic()>=data['deadline'] or (root/'stop-now').exists():break
            time.sleep(.05)
        for name,handle in last.items():
            p=subprocess.run([probe,'stop-path',name,handle],capture_output=True,timeout=3)
            # Parent may already have stopped it, absence is also safe.
            q=subprocess.run([probe,'query',name],capture_output=True,timeout=3)
            absent=q.returncode==0 and json.loads(q.stdout).get('absent') is True
            out[name]=dict(stop_return=p.returncode,absent=absent)
        (root/'watchdog-result.json').write_text(json.dumps(dict(sessions=out,complete=all(x['absent'] for x in out.values()))))
    except BaseException:
        (root/'watchdog-result.json').write_text(json.dumps(dict(complete=False,error='watchdog refusal')))
        raise
if __name__=='__main__':run(*sys.argv[1:])
