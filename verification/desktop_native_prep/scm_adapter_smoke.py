"""NEW unique-name SCM adapter smoke. Disposable Windows CI only, synthetic worker."""
import importlib.util,json,os,pathlib,shutil,subprocess,sys,time,types,uuid
HERE=pathlib.Path(__file__).resolve().parent

def service_mode(root,name):
    import servicemanager,win32service
    # Test-only dependencies isolate the EXACT service adapter from real app config,
    # certificate, database and server. They are not a production fallback.
    app=types.ModuleType('app');app.__path__=[];sys.modules['app']=app
    config=types.ModuleType('app.desktop_config')
    class Config:
        @staticmethod
        def load(path):return object()
    config.DesktopConfig=Config;sys.modules[config.__name__]=config
    diagnostics=types.ModuleType('app.desktop_diagnostics')
    diagnostics.failure_structure=lambda exc:json.dumps({'error_type':type(exc).__name__})
    sys.modules[diagnostics.__name__]=diagnostics
    server=types.ModuleType('app.desktop_server');server.config_path=lambda:None
    class Worker:
        should_exit=False
        force_exit=False
        def run(self):
            while not self.should_exit and not self.force_exit:
                (root/'runtime/heartbeat').write_text(str(time.monotonic()))
                time.sleep(.1)
    server.create_server=lambda _:Worker();sys.modules[server.__name__]=server
    spec=importlib.util.spec_from_file_location('hony_exact_service',root/'desktop_service.RECOVERED-EXACT.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    class Qualification(mod.HOnYService):
        _svc_name_=name
        _svc_display_name_=name
    servicemanager.Initialize();servicemanager.PrepareToHostSingle(Qualification)
    servicemanager.StartServiceCtrlDispatcher()

def parent():
    import win32service as ws,pywintypes
    if sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true':raise SystemExit('disposable Windows CI only')
    name='HOnYQualification'+uuid.uuid4().hex
    root=pathlib.Path(os.environ['ProgramFiles'])/name
    if root.exists():raise RuntimeError('fresh fixture root required')
    root.mkdir();(root/'runtime').mkdir()
    shutil.copy2(__file__,root/'scm_adapter_smoke.py')
    shutil.copy2(HERE/'desktop_service.RECOVERED-EXACT.py',root/'desktop_service.RECOVERED-EXACT.py')
    # Only owned fixture/runtime/venv ACLs. No Users/Everyone write grants, no
    # inheritance disabling, no base interpreter, hotel or production ACL changes.
    def acl(p,rights,recursive=False):
        command=['icacls.exe',str(p),'/grant','*S-1-5-20:'+rights]
        if recursive:command+=['/T']
        subprocess.run(command,check=True)
    acl(root,'(OI)(CI)RX',True);acl(root/'runtime','(OI)(CI)M',True)
    venv=pathlib.Path(sys.prefix)
    temp=pathlib.Path(os.environ['RUNNER_TEMP']).resolve()
    if not venv.resolve().is_relative_to(temp/'hony-desktop-native-prep'):raise RuntimeError('owned preparation venv required')
    acl(venv.parent,'RX');acl(venv,'(OI)(CI)RX',True)
    cmd=subprocess.list2cmdline([sys.executable,'-I',str(root/'scm_adapter_smoke.py'),'--service',str(root),name])
    scm=ws.OpenSCManager(None,None,ws.SC_MANAGER_CONNECT|ws.SC_MANAGER_CREATE_SERVICE)
    service=None
    def absence():
        try:h=ws.OpenService(scm,name,ws.SERVICE_QUERY_STATUS)
        except pywintypes.error as exc:
            if exc.winerror==1060:return True
            raise
        ws.CloseServiceHandle(h);return False
    if not absence():raise RuntimeError('unique service unexpectedly exists')
    def identity():
        c=ws.QueryServiceConfig(service)
        if c[0]!=ws.SERVICE_WIN32_OWN_PROCESS or c[1]!=ws.SERVICE_DEMAND_START or c[3]!=cmd or c[7].lower()!='nt authority\\networkservice':raise RuntimeError('owned service identity mismatch')
    def wait_state(state,seconds):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            status=ws.QueryServiceStatusEx(service)
            if status['CurrentState']==state:return status
            time.sleep(.2)
        raise RuntimeError('bounded service state timeout: '+str(state))
    try:
        service=ws.CreateService(scm,name,name,ws.SERVICE_ALL_ACCESS,ws.SERVICE_WIN32_OWN_PROCESS,
                                 ws.SERVICE_DEMAND_START,ws.SERVICE_ERROR_NORMAL,cmd,None,0,None,
                                 'NT AUTHORITY\\NetworkService',None)
        identity();ws.StartService(service,None);status=wait_state(ws.SERVICE_RUNNING,25)
        until=time.monotonic()+20
        while not (root/'runtime/heartbeat').exists():
            if time.monotonic()>until:raise RuntimeError('synthetic worker readiness timeout')
            time.sleep(.1)
        before=(root/'runtime/heartbeat').read_text()
        # Independent ordinary process ends. This proves process separation, NOT
        # visible desktop window interaction or actual HTTPS backend acceptance.
        subprocess.run([sys.executable,'-I','-c','pass'],check=True,timeout=10)
        time.sleep(1)
        after=(root/'runtime/heartbeat').read_text()
        if before==after or ws.QueryServiceStatusEx(service)['CurrentState']!=ws.SERVICE_RUNNING:raise RuntimeError('service failed ordinary process close independence')
        identity();ws.ControlService(service,ws.SERVICE_CONTROL_STOP);stopped=wait_state(ws.SERVICE_STOPPED,40)
        if stopped['Win32ExitCode'] or stopped['ServiceSpecificExitCode']:raise RuntimeError('intentional stop reported failure')
        print('NATIVE_SCM_EXACT_ADAPTER_SYNTHETIC_WORKER_PASS',json.dumps({'service':name,'account':'NetworkService','ordinary_process_independent':True,'visible_window_tested':False,'real_backend_tested':False,'intentional_stop_exit':0}),flush=True)
    finally:
        try:
            if service is not None:
                identity();state=ws.QueryServiceStatusEx(service)['CurrentState']
                if state!=ws.SERVICE_STOPPED:
                    if state!=ws.SERVICE_STOP_PENDING:ws.ControlService(service,ws.SERVICE_CONTROL_STOP)
                    wait_state(ws.SERVICE_STOPPED,40)
                identity();ws.DeleteService(service);ws.CloseServiceHandle(service);service=None
                until=time.monotonic()+15
                while not absence():
                    if time.monotonic()>until:raise RuntimeError('owned SCM removal unconfirmed')
                    time.sleep(.2)
                print('NATIVE_SCM_OWNED_SERVICE_REMOVAL_PASS',flush=True)
        finally:
            if service is not None:ws.CloseServiceHandle(service)
            ws.CloseServiceHandle(scm)

if __name__=='__main__':
    if len(sys.argv)==4 and sys.argv[1]=='--service':service_mode(pathlib.Path(sys.argv[2]),sys.argv[3])
    elif len(sys.argv)==1:parent()
    else:raise SystemExit('unsupported invocation')
