"""NEW actual HTTPS backend fixture, disposable native CI, synthetic config/DB/TLS only."""
import hashlib,http.client,ipaddress,json,os,pathlib,secrets,shutil,socket,ssl,subprocess,sys,time,uuid
HERE=pathlib.Path(__file__).resolve().parent

def run_child(root):
    sys.path.insert(0,str(root/'source'))
    from app.desktop_config import DesktopConfig
    from app.desktop_server import create_server
    config=DesktopConfig.load(root/'desktop.json')
    create_server(config).run()

def parent(scm_mode=False,visible=False,authenticated=False):
    if sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('disposable native CI required')
    import win32api,win32con,win32event,win32job,win32process
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID,ExtendedKeyUsageOID
    from datetime import datetime,timedelta,timezone
    root=pathlib.Path(os.environ['RUNNER_TEMP'])/('hony-actual-authenticated-fixture' if authenticated else ('hony-actual-visible-fixture' if visible else ('hony-actual-https-scm-fixture' if scm_mode else 'hony-actual-https-fixture')))
    if root.exists():raise RuntimeError('fresh fixture root required')
    root.mkdir();source=root/'source';source.mkdir()
    # Restore ONLY git-tracked public app consumer bytes, never checkout env/runtime.
    manifest=json.loads((HERE/'stage-source-manifest.json').read_bytes())
    for e in manifest['files']:
        blob=subprocess.check_output(['git','cat-file','blob','HEAD:'+e['path']])
        if hashlib.sha256(blob).hexdigest()!=e['sha256']:raise RuntimeError('source hash refused')
        p=source/e['path'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(blob)
    for name in ['desktop_config','desktop_server','desktop_certificate','desktop_security','desktop_window','desktop_service','desktop_diagnostics','desktop_webview_guard']:
        suffix='.CANDIDATE.py' if name=='desktop_webview_guard' else '.RECOVERED-EXACT.py'
        shutil.copy2(HERE/(name+suffix),source/'app'/(name+'.py'))
    addresses=[]
    for item in socket.getaddrinfo(socket.gethostname(),None,socket.AF_INET):
        addr=item[4][0]
        if any(ipaddress.ip_address(addr) in ipaddress.ip_network(n) for n in ['10.0.0.0/8','172.16.0.0/12','192.168.0.0/16']):addresses.append(addr)
    if not addresses:raise RuntimeError('runner has no policy-compatible RFC1918 IPv4')
    bind=sorted(set(addresses))[0]
    with socket.socket() as sock:sock.bind((bind,0));port=sock.getsockname()[1]
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));pgport=sock.getsockname()[1]
    hostname=('hony-'+uuid.uuid4().hex+'.invalid' if visible else 'hony-qualification.invalid');origin=f'https://{hostname}:{port}'
    now=datetime.now(timezone.utc)
    ca_key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    subject=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'Disposable fixture CA')])
    ca=(x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(ca_key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=2)).add_extension(x509.BasicConstraints(ca=True,path_length=0),critical=True).sign(ca_key,hashes.SHA256()))
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    leaf=(x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,hostname)])).issuer_name(subject).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5)).not_valid_after(now+timedelta(days=1)).add_extension(x509.BasicConstraints(ca=False,path_length=None),critical=True).add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]),critical=False).add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),critical=False).sign(ca_key,hashes.SHA256()))
    (root/'ca.pem').write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    (root/'leaf.pem').write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
    (root/'leaf.key').write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    # CA private key is never written. Only visible mode uses owned user-store/hosts fixture; never firewall.
    (root/'desktop.json').write_text(json.dumps(dict(origin=origin,bind_ipv4=bind,certificate=str(root/'leaf.pem'),private_key=str(root/'leaf.key'),project_root=str(source))))
    installations=sorted((pathlib.Path(os.environ['ProgramFiles'])/'PostgreSQL').glob('[0-9]*/bin'),key=lambda p:int(p.parent.name),reverse=True)
    if not installations:raise RuntimeError('preinstalled PostgreSQL required')
    pg=installations[0];cluster=root/'postgres';pw=root/'test-password';pw.write_text('synthetic-fixture-password')
    pgstarted=False;job=process=thread=None;output=None;owned_service=None;ca_owned=False;hosts_owned=False;hosts_before=None;hosts_written=None;ca_thumb=None
    env={**os.environ,'PGPASSWORD':'synthetic-fixture-password','APP_ENV':'production','DATABASE_URL':f'postgresql+psycopg://hotel_fixture:synthetic-fixture-password@127.0.0.1:{pgport}/hotel_fixture','SESSION_SECRET':secrets.token_hex(32),'LOGIN_ALERT_WEBHOOK_URL':''}
    (source/'.env').write_text('APP_ENV=production\nDATABASE_URL='+env['DATABASE_URL']+'\nSESSION_SECRET='+env['SESSION_SECRET']+'\n')
    try:
        subprocess.run([str(pg/'initdb.exe'),'-D',str(cluster),'-U','hotel_fixture','--encoding=UTF8','--locale=C','--auth=scram-sha-256','--pwfile',str(pw)],check=True,timeout=50)
        pw.unlink()
        subprocess.run([str(pg/'pg_ctl.exe'),'-D',str(cluster),'-l',str(root/'postgres.log'),'-o',f'-h 127.0.0.1 -p {pgport}','-w','start'],check=True,timeout=50);pgstarted=True
        subprocess.run([str(pg/'createdb.exe'),'-h','127.0.0.1','-p',str(pgport),'-U','hotel_fixture','hotel_fixture'],check=True,env=env,timeout=15)
        # Real migrations using exact application source, no system Python path guess.
        subprocess.run([sys.executable,'-I','-c','import sys,runpy;sys.path.insert(0,sys.argv.pop(1));sys.argv=["alembic","upgrade","head"];runpy.run_module("alembic",run_name="__main__")',str(source)],cwd=source,env=env,check=True,timeout=60)
        if authenticated:
            # Already independently reviewed/migrated-DB-qualified helper, immutable peer bytes.
            env['FIXTURE_ADMIN_USERNAME']='fixture_admin'
            env['FIXTURE_ADMIN_PASSWORD']=secrets.token_urlsafe(24)
            env['FIXTURE_DATABASE_URL']=env['DATABASE_URL']
            for variable in ['TELEGRAM_BOT_TOKEN','TELEGRAM_CHAT_ID']:env.pop(variable,None)
            result=subprocess.run([sys.executable,'-I',str(HERE/'auth_fixture/seed_fixture.py'),'--source-root',str(source),'--username',env['FIXTURE_ADMIN_USERNAME'],'--start-date','2026-03-01','--end-date','2026-03-31'],cwd=source,env=env,capture_output=True,text=True,timeout=30)
            if result.returncode!=0:raise RuntimeError('authenticated seed refused; output deliberately not logged')
            seed=json.loads(result.stdout)
            if not seed.get('ok'):raise RuntimeError('authenticated seed success unconfirmed')
            (root/'auth-seed.json').write_text(json.dumps(seed))
            print('NATIVE_AUTH_REAL_PAYROLL_SEED_PASS SEEDED_APPROVAL_PREREQUISITES_ONLY',flush=True)
        if scm_mode:
            import importlib.util
            shutil.copy2(HERE/'actual_scm_controller.py',root/'actual_scm_controller.py')
            spec=importlib.util.spec_from_file_location('controller',HERE/'actual_scm_controller.py')
            controller=importlib.util.module_from_spec(spec);spec.loader.exec_module(controller)
            controller.prepare_acl(root);owned_service=controller.OwnedService(root);owned_service.start()
        else:
            win32api.SetLastError(0);job=win32job.CreateJobObject(None,'Local\\HOnY-https-'+uuid.uuid4().hex)
            if win32api.GetLastError()==183:raise RuntimeError('fresh Job required')
            info=win32job.QueryInformationJobObject(job,win32job.JobObjectExtendedLimitInformation);info['BasicLimitInformation']['LimitFlags']|=win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;win32job.SetInformationJobObject(job,win32job.JobObjectExtendedLimitInformation,info)
            command=subprocess.list2cmdline([sys.executable,'-I',str(HERE/'actual_https_smoke.py'),'--child',str(root)])
            startup=win32process.STARTUPINFO()
            process,thread,pid,tid=win32process.CreateProcess(sys.executable,command,None,None,False,win32con.CREATE_SUSPENDED,env,str(source),startup)
            win32job.AssignProcessToJobObject(job,process)
            if win32process.ResumeThread(thread)!=1:raise RuntimeError('prior suspend count refused')
        context=ssl.create_default_context(cafile=str(root/'ca.pem'))
        def request(path='/health',host=None,origin_header=None,ctx=context):
            raw=socket.create_connection((bind,port),timeout=4)
            try:
                tls=ctx.wrap_socket(raw,server_hostname=hostname)
            except BaseException:raw.close();raise
            with tls:
                data='GET '+path+' HTTP/1.1\r\nHost: '+(host or f'{hostname}:{port}')+'\r\nConnection: close\r\n'
                if origin_header:data+='Origin: '+origin_header+'\r\n'
                tls.sendall((data+'\r\n').encode('ascii'));resp=http.client.HTTPResponse(tls);resp.begin();return resp.status,dict(resp.getheaders()),resp.read()
        until=time.monotonic()+35
        while True:
            try:status,headers,body=request();break
            except (OSError,ssl.SSLError):
                if process is not None and win32event.WaitForSingleObject(process,0)==win32con.WAIT_OBJECT_0:raise RuntimeError('actual backend child exited '+str(win32process.GetExitCodeProcess(process)))
                if time.monotonic()>until:raise RuntimeError('actual HTTPS bounded readiness failed')
                time.sleep(.2)
        assert status==200 and json.loads(body)=={'status':'ok','database':'connected'}
        assert headers.get('cache-control')=='no-store, private'
        assert request(host='foreign.invalid')[0]==403
        assert request(origin_header='https://foreign.invalid')[0]==403
        assert request(origin_header=origin)[0]==200
        try:request(ctx=ssl.create_default_context())
        except ssl.SSLCertVerificationError:pass
        else:raise RuntimeError('untrusted fixture leaf unexpectedly accepted')
        if scm_mode:
            subprocess.run([sys.executable,'-I','-c','pass'],check=True,timeout=10)
            assert owned_service.running() and request()[0]==200
            print('NATIVE_ACTUAL_HTTPS_SCM_NETWORKSERVICE_INDEPENDENT_PROCESS_PASS',flush=True)
        if visible:
            ca_thumb=ca.fingerprint(hashes.SHA1()).hex().upper()
            def trust(mode):
                # Bound shell AND certutil descendants, including partial-import timeouts.
                tj=tp=tt=None
                try:
                    win32api.SetLastError(0);tj=win32job.CreateJobObject(None,'Local\\HOnY-trust-'+uuid.uuid4().hex)
                    if win32api.GetLastError()==183:raise RuntimeError('fresh trust Job required')
                    limits=win32job.QueryInformationJobObject(tj,win32job.JobObjectExtendedLimitInformation)
                    limits['BasicLimitInformation']['LimitFlags']|=win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                    win32job.SetInformationJobObject(tj,win32job.JobObjectExtendedLimitInformation,limits)
                    exe=str(pathlib.Path(os.environ['SystemRoot'])/'System32/WindowsPowerShell/v1.0/powershell.exe')
                    cmd=subprocess.list2cmdline([exe,'-NoProfile','-NonInteractive','-File',str(HERE/'trust_fixture.ps1'),'-Mode',mode,'-Thumbprint',ca_thumb,'-CertificatePath',str(root/'ca.pem')])
                    tp,tt,_,_=win32process.CreateProcess(exe,cmd,None,None,False,win32con.CREATE_SUSPENDED,None,None,win32process.STARTUPINFO())
                    win32job.AssignProcessToJobObject(tj,tp)
                    if win32process.ResumeThread(tt)!=1:raise RuntimeError('trust prior suspend count refused')
                    trust_until=time.monotonic()+20;confirmed=False
                    while win32event.WaitForSingleObject(tp,200)!=win32con.WAIT_OBJECT_0 and time.monotonic()<trust_until:
                        if mode not in ('import','remove') or confirmed:continue
                        title='Security Warning' if mode=='import' else 'Root Certificate Store'
                        import re,win32gui
                        members=set(win32job.QueryInformationJobObject(tj,win32job.JobObjectBasicProcessIdList));dialogs=[]
                        def find_confirmation(hwnd,_):
                            _,owner=win32process.GetWindowThreadProcessId(hwnd)
                            if owner in members and win32gui.IsWindowVisible(hwnd) and win32gui.GetClassName(hwnd)=='#32770' and win32gui.GetWindowText(hwnd)==title:dialogs.append(hwnd)
                        win32gui.EnumWindows(find_confirmation,None)
                        if not dialogs:continue
                        if len(dialogs)!=1:raise RuntimeError('owned CA confirmation ambiguous')
                        hwnd=dialogs[0];labels=[]
                        win32gui.EnumChildWindows(hwnd,lambda child,_:labels.append(win32gui.GetWindowText(child)),None)
                        text='\n'.join(labels)
                        if mode=='remove':
                            # Fixed structural evidence only: preserve letters in SHA1 label,
                            # map all hex digits to H and non-label letters to X.
                            at=text.lower().find('sha1')
                            print('NATIVE_REMOVE_SHA1_STRUCTURE',json.dumps({'sha1_offset':at,'label_tail_codes':[ord(c) for c in text[max(0,at-15):at+8]] if at>=0 else [],'following_shape':''.join('H' if c in '0123456789abcdefABCDEF' else ('W' if c.isspace() else ('X' if c.isalpha() else c)) for c in text[at+4:at+110]) if at>=0 else '', 'controls':len(labels)}),flush=True)
                        match=re.search(r'Thumbprint \(sha1\)\s*:\s*([0-9A-Fa-f\s]{40,100})',text)
                        observed=''.join(match.group(1).split()).upper() if match else ''
                        print('NATIVE_TRUST_DIALOG_GUARD',json.dumps({'mode':mode,'matched':bool(match),'digits':len(observed),'exact':observed==ca_thumb,'label':'Disposable fixture CA' in text}),flush=True)
                        if not match or observed!=ca_thumb or 'Disposable fixture CA' not in text:raise RuntimeError('owned CA confirmation fingerprint mismatch')
                        yes=win32gui.GetDlgItem(hwnd,6) # Documented WinUser IDYES.
                        print('NATIVE_TRUST_DIALOG_BUTTON',json.dumps({'mode':mode,'idyes_present':bool(yes),'exact_yes':bool(yes) and win32gui.GetWindowText(yes).replace('&','')=='Yes'}),flush=True)
                        if not yes or win32gui.GetWindowText(yes).replace('&','')!='Yes':raise RuntimeError('exact CA confirmation button missing')
                        win32gui.SendMessage(yes,0x00F5,0,0) # BM_CLICK only on exact owned IDYES.
                        confirmed=True;print('NATIVE_TRUST_EXACT_OWNED_CA_OS_CONFIRMATION_PASS',mode,flush=True)
                    if win32event.WaitForSingleObject(tp,0)!=win32con.WAIT_OBJECT_0:
                        # Observe only windows whose process is in THIS owned helper Job.
                        import win32gui,win32process as wp
                        from PIL import ImageGrab
                        proof=pathlib.Path(os.environ['RUNNER_TEMP'])/'hony-trust-observation'
                        proof.mkdir(exist_ok=False)
                        owned=set(win32job.QueryInformationJobObject(tj,win32job.JobObjectBasicProcessIdList))
                        windows=[]
                        def inspect(hwnd,_):
                            _,pid=wp.GetWindowThreadProcessId(hwnd)
                            if pid in owned and win32gui.IsWindowVisible(hwnd):windows.append(hwnd)
                        win32gui.EnumWindows(inspect,None)
                        captured=0
                        for hwnd in windows[:4]:
                            box=win32gui.GetWindowRect(hwnd)
                            if 20<=box[2]-box[0]<=2000 and 20<=box[3]-box[1]<=1600:
                                ImageGrab.grab(bbox=box,all_screens=True).save(proof/('owned-window-'+str(captured)+'.png'));captured+=1
                        (proof/'result.json').write_text(json.dumps({'mode':mode,'owned_processes':len(owned),'visible_owned_windows':len(windows),'captured':captured,'deadline_seconds':20,'no_automatic_click':True}))
                        raise RuntimeError('trust operation deadline; bounded owned-window observation retained')
                    if win32process.GetExitCodeProcess(tp)!=0:raise RuntimeError('trust operation refused')
                finally:
                    try:
                        if tj is not None:win32job.TerminateJobObject(tj,99)
                        if tp is not None and win32event.WaitForSingleObject(tp,5000)!=win32con.WAIT_OBJECT_0:
                            win32process.TerminateProcess(tp,99)
                            if win32event.WaitForSingleObject(tp,5000)!=win32con.WAIT_OBJECT_0:raise RuntimeError('trust child cleanup unconfirmed')
                        if tj is not None:
                            deadline=time.monotonic()+5
                            while win32job.QueryInformationJobObject(tj,win32job.JobObjectBasicAccountingInformation)['ActiveProcesses']:
                                if time.monotonic()>deadline:raise RuntimeError('trust Job cleanup unconfirmed')
                                time.sleep(.1)
                            print('NATIVE_TRUST_OPERATION_OWNED_JOB_CLEANUP_PASS',mode,flush=True)
                    finally:
                        for handle in [tt,tp,tj]:
                            if handle is not None:handle.Close()
            hosts_path=pathlib.Path(os.environ['SystemRoot'])/'System32/drivers/etc/hosts'
            hosts_before=hosts_path.read_bytes()
            if hostname.encode('ascii') in hosts_before:raise RuntimeError('unexpected hosts name already present')
            trust('absent')
            # Mark owned before import so partial import cannot silently escape cleanup.
            ca_owned=True;trust('import')
            marker='HOnY-owned-'+uuid.uuid4().hex
            hosts_written=hosts_before+('\r\n'+bind+' '+hostname+' # '+marker+'\r\n').encode('ascii')
            hosts_owned=True;hosts_path.write_bytes(hosts_written)
            if hosts_path.read_bytes()!=hosts_written:raise RuntimeError('owned hosts append unconfirmed')
            if bind not in [x[4][0] for x in socket.getaddrinfo(hostname,None,socket.AF_INET)]:raise RuntimeError('owned hosts resolution unconfirmed')
            profile=root/'window-profile';profile.mkdir();localenv={**env,'LOCALAPPDATA':str(profile)}
            # Dedicated owned Job bounds the actual window and WebView descendants.
            win32api.SetLastError(0);job=win32job.CreateJobObject(None,'Local\\HOnY-visible-'+uuid.uuid4().hex)
            if win32api.GetLastError()==183:raise RuntimeError('fresh visible Job required')
            info=win32job.QueryInformationJobObject(job,win32job.JobObjectExtendedLimitInformation);info['BasicLimitInformation']['LimitFlags']|=win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;win32job.SetInformationJobObject(job,win32job.JobObjectExtendedLimitInformation,info)
            driver='authenticated_window_smoke.py' if authenticated else 'visible_window_smoke.py'
            command=subprocess.list2cmdline([sys.executable,'-I',str(HERE/driver),str(root)])
            process,thread,pid,tid=win32process.CreateProcess(sys.executable,command,None,None,False,win32con.CREATE_SUSPENDED,localenv,str(source),win32process.STARTUPINFO())
            win32job.AssignProcessToJobObject(job,process)
            if win32process.ResumeThread(thread)!=1:raise RuntimeError('visible prior suspend count refused')
            if win32event.WaitForSingleObject(process,75000)!=win32con.WAIT_OBJECT_0:raise RuntimeError('visible window deadline')
            if win32process.GetExitCodeProcess(process)!=0:
                # Retain only known synthetic result/pixels, never DB/config/keys.
                failed=pathlib.Path(os.environ['RUNNER_TEMP'])/'hony-visible-failure-proof'
                failed.mkdir(exist_ok=False)
                for name in ['visible-window-result.json','visible-login.png','visible-popup.png','authenticated-window-result.json','authenticated-dirty-confirm.png']:
                    if (root/name).is_file():shutil.copy2(root/name,failed/name)
                raise RuntimeError('visible actual window child failed')
            result_name='authenticated-window-result.json' if authenticated else 'visible-window-result.json'
            result=json.loads((root/result_name).read_text())
            expected='authenticated-pages-rendered' if authenticated else 'visible-login-rendered'
            image_names=['authenticated-dashboard.png','authenticated-payroll.png','authenticated-report.png','authenticated-dirty-confirm.png'] if authenticated else ['visible-login.png','visible-popup.png']
            if result.get('status')!=expected or any(not (root/name).is_file() for name in image_names):raise RuntimeError('visible pixels/result missing')
            assert owned_service.running() and request()[0]==200
            evidence=pathlib.Path(os.environ['RUNNER_TEMP'])/('hony-authenticated-proof' if authenticated else 'hony-visible-proof')
            if evidence.exists():raise RuntimeError('fresh proof output required')
            if authenticated:
                subprocess.run([sys.executable,'-I',str(HERE/'authenticated_state_check.py'),str(root)],cwd=source,env=env,check=True,timeout=20)
            evidence.mkdir();shutil.copy2(root/result_name,evidence/result_name)
            for name in image_names:shutil.copy2(root/name,evidence/name)
            print('NATIVE_ACTUAL_VISIBLE_CLOSE_SERVICE_HEALTH_PASS',flush=True)
        print('NATIVE_ACTUAL_HTTPS_BACKEND_DB_TLS_PASS',json.dumps({'production_settings':True,'real_app_health_database':'connected','strict_CA_hostname':True,'untrusted_CA_refused':True,'foreign_host_origin_refused':True,'no_store':True,'SCM_combined':scm_mode,'visible_window':visible}),flush=True)
    finally:
        # Independent cleanup phases: a child error must never skip owned PG stop.
        cleanup_errors=[]
        try:
            if job is not None:win32job.TerminateJobObject(job,99)
            if process is not None:
                if win32event.WaitForSingleObject(process,5000)!=win32con.WAIT_OBJECT_0:
                    try:win32process.TerminateProcess(process,99)
                    except Exception:
                        if win32event.WaitForSingleObject(process,5000)!=win32con.WAIT_OBJECT_0:raise
                    if win32event.WaitForSingleObject(process,5000)!=win32con.WAIT_OBJECT_0:raise RuntimeError('owned HTTPS child cleanup unconfirmed')
            if job is not None:
                until=time.monotonic()+5
                while win32job.QueryInformationJobObject(job,win32job.JobObjectBasicAccountingInformation)['ActiveProcesses']:
                    if time.monotonic()>until:raise RuntimeError('owned HTTPS Job cleanup unconfirmed')
                    time.sleep(.1)
                print('NATIVE_ACTUAL_HTTPS_OWNED_JOB_CLEANUP_PASS',flush=True)
        except BaseException as exc:cleanup_errors.append('child-cleanup:'+type(exc).__name__)
        finally:
            for h in [thread,process,job]:
                if h is not None:h.Close()
        try:
            if owned_service is not None:owned_service.close()
        except BaseException as exc:cleanup_errors.append('SCM-cleanup:'+type(exc).__name__)
        try:
            if pgstarted:
                subprocess.run([str(pg/'pg_ctl.exe'),'-D',str(cluster),'-m','fast','-w','stop'],check=True,timeout=40)
                result=subprocess.run([str(pg/'pg_ctl.exe'),'-D',str(cluster),'status'],capture_output=True)
                if result.returncode!=3:raise RuntimeError('owned synthetic PostgreSQL stop unconfirmed')
                print('NATIVE_ACTUAL_HTTPS_OWNED_POSTGRES_STOP_PASS',flush=True)
        except BaseException as exc:cleanup_errors.append('postgres-cleanup:'+type(exc).__name__)
        try:
            if hosts_owned:
                if hosts_path.read_bytes()!=hosts_written:raise RuntimeError('hosts changed outside owned fixture; refusing overwrite')
                hosts_path.write_bytes(hosts_before)
                if hosts_path.read_bytes()!=hosts_before:raise RuntimeError('hosts restoration unconfirmed')
                print('NATIVE_VISIBLE_HOSTS_EXACT_RESTORE_PASS',flush=True)
        except BaseException as exc:cleanup_errors.append('hosts-cleanup:'+type(exc).__name__)
        try:
            if ca_owned:
                trust('remove');trust('absent')
                print('NATIVE_VISIBLE_CURRENTUSER_CA_EXACT_REMOVAL_PASS',flush=True)
        except BaseException as exc:
            print('NATIVE_CA_CLEANUP_FAILURE_FIXED_CONTEXT',type(exc).__name__,str(exc) if isinstance(exc,RuntimeError) else 'external-error-redacted',flush=True)
            cleanup_errors.append('CA-cleanup:'+type(exc).__name__)
        if cleanup_errors:raise RuntimeError(';'.join(cleanup_errors))

if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--child':run_child(pathlib.Path(sys.argv[2]))
    elif len(sys.argv)==1:parent()
    elif sys.argv[1:]==['--scm']:parent(scm_mode=True)
    elif sys.argv[1:]==['--visible']:parent(scm_mode=True,visible=True)
    elif sys.argv[1:]==['--authenticated']:parent(scm_mode=True,visible=True,authenticated=True)
    else:raise SystemExit('unsupported invocation')
