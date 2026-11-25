"""REVIEW ONLY. Scoped session orchestration, no command-line entrypoint.
Caller must supply exact baseline callable and child identity evidence; session
contents never leave owned root. Do not invoke before parent review.
"""
import subprocess,pathlib,json,os,time,sys,uuid
from xml_adapter import load
from correlate import Refusal
NAMES=('HOnYParserFile','HOnYParserProcess')
PROVIDERS=('edd08927-9cc4-4e65-b970-c2560fb5c289','22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716')

def invoke(argv,timeout=3):
    p=subprocess.run(argv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout,check=False)
    if len(p.stdout)+len(p.stderr)>65536:raise Refusal('command output cap')
    if p.returncode:raise Refusal('command refused, raw diagnostics withheld')
    return p.stdout

def query(probe,name):return json.loads(invoke([str(probe),'query',name]))

def record_once(probe,parent_root,workload):
    """workload runs unchanged owned startup+control once and returns owner metadata.
    No retries. Caller only gets normalized records after raw absence confirmed.
    """
    if os.name!='nt' or os.environ.get('GITHUB_ACTIONS')!='true':raise Refusal('disposable Windows CI only')
    probe=pathlib.Path(probe).resolve(strict=True);parent_root=pathlib.Path(parent_root).resolve(strict=True)
    runner_temp=pathlib.Path(os.environ['RUNNER_TEMP']).resolve(strict=True)
    if not parent_root.is_relative_to(runner_temp) or parent_root.is_symlink() or probe.is_symlink():raise Refusal('owned runner root required')
    root=parent_root/('trace-'+uuid.uuid4().hex);root.mkdir()
    raw=[root/'file.etl',root/'process.etl',root/'events.xml',root/'summary.txt']
    journal=root/'journal.json';wd=None;metadata=None;rows=None;losses=[];stopped=[];error=None
    def arm():
        data=dict(owner_pid=os.getpid(),deadline=time.monotonic()+10,sessions={n:str(p) for n,p in zip(NAMES,raw)})
        tmp=root/'journal.tmp';tmp.write_text(json.dumps(data));tmp.replace(journal)
    try:
        for name in NAMES:
            if query(probe,name)!={'absent':True}:raise Refusal('existing session, refuse without disturbance')
        arm() # prearm both exact ETL names before start, closes owner-crash startup gap
        with (root/'watchdog-output').open('wb') as out:
            wd=subprocess.Popen([sys.executable,str(pathlib.Path(__file__).with_name('watchdog.py')),str(probe),str(journal),str(root)],stdout=out,stderr=out)
        until=time.monotonic()+2
        while not (root/'watchdog-ready').is_file():
            if wd.poll() is not None or time.monotonic()>until:raise Refusal('watchdog readiness missing')
            time.sleep(.025)
        started=time.monotonic()
        for name,provider,keyword,path in zip(NAMES,PROVIDERS,('0xe0','0x30'),raw):
            invoke(['logman.exe','create','trace',name,'-o',str(path),'-f','bincirc','-max','64','-bs','64','-nb','16','64','-p','{'+provider+'}',keyword,'5','-ets'])
            info=query(probe,name)
            if info.get('absent') is not False or not info.get('handle'):raise Refusal('new session identity missing')
        metadata=workload()
        if time.monotonic()-started>5:raise Refusal('five-second recording scope exceeded')
    except BaseException as e:error=e
    finally:
        for name,path in zip(NAMES,raw):
            try:
                q=query(probe,name)
                if q.get('absent') is not True:
                    info=json.loads(invoke([str(probe),'stop-path',name,str(path)]))
                    if info.get('stopped') is not True:raise Refusal('exact owned stop unconfirmed')
                    losses.append(sum(info[k] for k in ('events_lost','buffers_lost','realtime_lost')))
                else:losses.append(None) # unknown stop statistics can never pass correlation
                if query(probe,name)!={'absent':True}:raise Refusal('session absence unconfirmed')
                stopped.append(name)
            except BaseException as e:error=e
        (root/'stop-now').touch()
        if wd:
            try:
                wd.wait(timeout=7)
                result=json.loads((root/'watchdog-result.json').read_text())
                if not result.get('complete') or set(result.get('sessions',{}))!=set(NAMES):raise Refusal('independent watchdog absence unconfirmed')
            except BaseException as e:error=e
        if not error:
            try:
                # Conversion is separately polled for output cap, never console raw paths.
                with (root/'convert-output').open('wb') as out:
                    p=subprocess.Popen(['tracerpt.exe',str(raw[0]),str(raw[1]),'-o',str(raw[2]),'-of','XML','-summary',str(raw[3]),'-y'],stdout=out,stderr=out)
                    deadline=time.monotonic()+10
                    try:
                        while p.poll() is None:
                            if time.monotonic()>deadline or any(x.exists() and x.stat().st_size>268435456 for x in raw) or (root/'convert-output').stat().st_size>65536:raise Refusal('conversion cap')
                            time.sleep(.05)
                        if p.returncode:raise Refusal('conversion refused')
                    finally:
                        if p.poll() is None:p.kill();p.wait(timeout=3)
                rows=load(raw[2])
            except BaseException as e:error=e
        # Only explicitly created direct children in this fresh private root.
        for path in list(root.iterdir()):
            if path.is_dir() or path.is_symlink():error=Refusal('unexpected trace root entry');continue
            try:path.unlink()
            except BaseException as e:error=e
        if any(root.iterdir()):error=Refusal('raw trace absence unconfirmed')
        else:root.rmdir()
    if error:raise Refusal('bounded trace inconclusive; no retry; raw cleanup attempted') from error
    if stopped!=list(NAMES) or len(losses)!=2 or any(x!=0 for x in losses):raise Refusal('loss statistics missing or nonzero')
    return rows,metadata,0
