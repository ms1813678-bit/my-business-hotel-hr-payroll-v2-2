"""NEW actual backend SCM controller, disposable unique service only."""
import json,os,pathlib,subprocess,sys,time,uuid

def service_mode(root,name):
    sys.path.insert(0,str(root/'source'))
    import servicemanager
    import app.desktop_service as module
    # Fixture-only configuration location. Exact real adapter/server/config/TLS
    # and actual application/DB code remain unchanged.
    module.config_path=lambda:root/'desktop.json'
    class Qualification(module.HOnYService):
        _svc_name_=name
        _svc_display_name_=name
    servicemanager.Initialize();servicemanager.PrepareToHostSingle(Qualification)
    servicemanager.StartServiceCtrlDispatcher()

class OwnedService:
    def __init__(self,root):
        import win32service as ws,pywintypes
        self.ws=ws;self.error=pywintypes.error;self.root=root
        self.name='HOnYActualQualification'+uuid.uuid4().hex;self.handle=None
        self.scm=ws.OpenSCManager(None,None,ws.SC_MANAGER_CONNECT|ws.SC_MANAGER_CREATE_SERVICE)
        self.cmd=subprocess.list2cmdline([sys.executable,'-I',str(root/'actual_scm_controller.py'),'--service',str(root),self.name])
    def absent(self):
        try:h=self.ws.OpenService(self.scm,self.name,self.ws.SERVICE_QUERY_STATUS)
        except self.error as exc:
            if exc.winerror==1060:return True
            raise
        self.ws.CloseServiceHandle(h);return False
    def identity(self):
        c=self.ws.QueryServiceConfig(self.handle)
        if c[0]!=self.ws.SERVICE_WIN32_OWN_PROCESS or c[1]!=self.ws.SERVICE_DEMAND_START or c[3]!=self.cmd or c[7].lower()!='nt authority\\networkservice':raise RuntimeError('owned real service identity mismatch')
    def wait(self,state,seconds):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            s=self.ws.QueryServiceStatusEx(self.handle)
            if s['CurrentState']==state:return s
            time.sleep(.2)
        raise RuntimeError('owned real SCM state deadline')
    def start(self):
        ws=self.ws
        if not self.absent():raise RuntimeError('fresh unique SCM name required')
        self.handle=ws.CreateService(self.scm,self.name,self.name,ws.SERVICE_ALL_ACCESS,ws.SERVICE_WIN32_OWN_PROCESS,ws.SERVICE_DEMAND_START,ws.SERVICE_ERROR_NORMAL,self.cmd,None,0,None,'NT AUTHORITY\\NetworkService',None)
        self.identity();ws.StartService(self.handle,None);self.wait(ws.SERVICE_RUNNING,25)
    def running(self):return self.ws.QueryServiceStatusEx(self.handle)['CurrentState']==self.ws.SERVICE_RUNNING
    def close(self):
        ws=self.ws
        try:
            if self.handle is not None:
                self.identity();state=ws.QueryServiceStatusEx(self.handle)['CurrentState']
                if state!=ws.SERVICE_STOPPED:
                    if state!=ws.SERVICE_STOP_PENDING:ws.ControlService(self.handle,ws.SERVICE_CONTROL_STOP)
                    self.wait(ws.SERVICE_STOPPED,40)
                state=ws.QueryServiceStatusEx(self.handle)
                if state['Win32ExitCode'] or state['ServiceSpecificExitCode']:raise RuntimeError('owned real service stop reported failure')
                self.identity();ws.DeleteService(self.handle);ws.CloseServiceHandle(self.handle);self.handle=None
                deadline=time.monotonic()+15
                while not self.absent():
                    if time.monotonic()>deadline:raise RuntimeError('owned real service removal unconfirmed')
                    time.sleep(.2)
                print('NATIVE_ACTUAL_HTTPS_SCM_STOP_ZERO_REMOVAL_PASS',flush=True)
        finally:
            if self.handle is not None:ws.CloseServiceHandle(self.handle)
            ws.CloseServiceHandle(self.scm)

def prepare_acl(root):
    def grant(p,rights,recurse=False):
        args=['icacls.exe',str(p),'/grant','*S-1-5-20:'+rights]
        if recurse:args+=['/T']
        subprocess.run(args,check=True)
    # Only owned fixture paths; read config/key, modify only runtime var.
    grant(root,'RX');grant(root/'actual_scm_controller.py','R');grant(root/'source','(OI)(CI)RX',True)
    for p in ['desktop.json','leaf.pem','leaf.key']:grant(root/p,'R')
    var=root/'source/var';var.mkdir(exist_ok=True);grant(var,'(OI)(CI)M',True)
    venv=pathlib.Path(sys.prefix)
    if not venv.resolve().is_relative_to(pathlib.Path(os.environ['RUNNER_TEMP']).resolve()/'hony-desktop-native-prep'):raise RuntimeError('owned venv required')
    grant(venv.parent,'RX');grant(venv,'(OI)(CI)RX',True)

if __name__=='__main__':
    if len(sys.argv)==4 and sys.argv[1]=='--service':service_mode(pathlib.Path(sys.argv[2]),sys.argv[3])
    else:raise SystemExit('service-only fixture entry')
