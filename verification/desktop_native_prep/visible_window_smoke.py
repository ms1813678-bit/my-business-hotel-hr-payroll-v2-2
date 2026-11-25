"""NEW visible native exact production window fixture, synthetic CI only."""
import json,os,pathlib,sys,time,threading

def run(root):
    if sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('disposable native CI only')
    sys.path.insert(0,str(root/'source'))
    import webview
    from app.desktop_window import run_window
    origin=json.loads((root/'desktop.json').read_text())['origin']
    original=webview.create_window
    from app.desktop_webview_guard import WebView2OriginGuard
    from app.desktop_webview_guard import _NativeGuardedPopup
    # Test-only tracing observes suppressed native exceptions without changing
    # the recovered method's flow, handlers, fallback or production bytes.
    original_initialized=_NativeGuardedPopup._on_initialized
    original_fail=_NativeGuardedPopup.fail
    def observe_call(method,self,*args):
        prior=sys.gettrace()
        def trace(frame,event,arg):
            if event=='exception' and pathlib.Path(frame.f_code.co_filename).name=='desktop_webview_guard.py':
                print('NATIVE_POPUP_FIXED_EXCEPTION',frame.f_code.co_name,frame.f_lineno,arg[0].__name__,flush=True)
            return trace
        sys.settrace(trace)
        try:return method(self,*args)
        finally:sys.settrace(prior)
    def observed_initialized(self,*args):return observe_call(original_initialized,self,*args)
    def observed_fail(self,*args):return observe_call(original_fail,self,*args)
    _NativeGuardedPopup._on_initialized=observed_initialized
    _NativeGuardedPopup.fail=observed_fail
    guards=[];original_guard_init=WebView2OriginGuard.__init__
    def observed_guard_init(self,*args,**kwargs):
        original_guard_init(self,*args,**kwargs);guards.append(self)
    WebView2OriginGuard.__init__=observed_guard_init
    evidence={};observed_popups=[]
    def observed_create(*args,**kwargs):
        window=original(*args,**kwargs)
        def loaded():
            try:
                # Production create/start/guard/navigation/TLS are unchanged.
                until=time.monotonic()+15
                while time.monotonic()<until:
                    data=window.evaluate_js('JSON.stringify({url:location.href,ready:document.readyState,forms:document.forms.length,passwords:document.querySelectorAll("input[type=password]").length})')
                    parsed=json.loads(data) if isinstance(data,str) else data
                    if parsed.get('ready')=='complete' and parsed.get('passwords',0)>0:break
                    time.sleep(.2)
                else:raise RuntimeError('actual login DOM readiness unconfirmed')
                if not parsed['url'].startswith(origin+'/'):raise RuntimeError('window actual origin mismatch')
                from PIL import ImageGrab
                import win32gui
                # Find THIS exact top-level app window; no full desktop capture.
                candidates=[]
                def each(hwnd,_):
                    if win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd)=='HOnY':candidates.append(hwnd)
                win32gui.EnumWindows(each,None)
                if len(candidates)!=1:raise RuntimeError('exact visible app window identity ambiguous')
                hwnd=candidates[0];win32gui.SetForegroundWindow(hwnd);time.sleep(.5)
                box=win32gui.GetWindowRect(hwnd)
                image=ImageGrab.grab(bbox=box,all_screens=True)
                image.save(root/'visible-login.png')
                evidence.update(status='visible-login-rendered',password_inputs=parsed['passwords'],forms=parsed['forms'],exact_origin=True,width=image.width,height=image.height,normal_TLS=True)
                root_guard=next(g for g in guards if g._window is window)
                window.evaluate_js('document.querySelector("input[name=username]").value="synthetic-unsaved-marker";window.open(location.origin+"/login","_blank")')
                until=time.monotonic()+15;popup=None
                while time.monotonic()<until:
                    candidates=[p for p in root_guard._popups if p.handed_off and not p.closed]
                    if len(candidates)==1:
                        popup=candidates[0]
                        if popup not in observed_popups:observed_popups.append(popup)
                        if str(popup.control.CoreWebView2.Source).startswith(origin+'/login'):break
                    time.sleep(.1)
                else:raise RuntimeError('actual guarded popup handoff missing')
                # Native popup must retain exact production environment/profile.
                core=popup.control.CoreWebView2;opener=root_guard._control.CoreWebView2
                if core.Environment!=opener.Environment or core.Profile.ProfileName!=opener.Profile.ProfileName:raise RuntimeError('popup environment/profile mismatch')
                if root_guard.opened_new_windows!=1:raise RuntimeError('popup open count mismatch')
                if window.evaluate_js('document.querySelector("input[name=username]").value')!='synthetic-unsaved-marker':raise RuntimeError('parent unsaved marker replaced')
                until=time.monotonic()+10
                while time.monotonic()<until:
                    holder=[]
                    root_guard._marshal_to_ui(popup.control,lambda:holder.append(core.ExecuteScriptAsync('JSON.stringify({ready:document.readyState,passwords:document.querySelectorAll("input[type=password]").length})')))
                    result=json.loads(json.loads(str(holder[0].Result)))
                    if result.get('ready')=='complete' and result.get('passwords')==1:break
                    time.sleep(.1)
                else:raise RuntimeError('popup actual login DOM missing')
                phwnd=int(popup.form.Handle.ToInt64());box=win32gui.GetWindowRect(phwnd)
                ImageGrab.grab(bbox=box,all_screens=True).save(root/'visible-popup.png')
                window.evaluate_js('window.open("https://foreign-popup.invalid/","_blank")')
                until=time.monotonic()+5
                while root_guard.blocked_new_windows!=1:
                    if time.monotonic()>until:raise RuntimeError('foreign popup guard refusal missing')
                    time.sleep(.1)
                if root_guard.opened_new_windows!=1 or len(root_guard._popups)!=1:raise RuntimeError('foreign popup created native view')
                evidence.update(popup_same_origin_login_rendered=True,popup_shared_environment_profile=True,parent_unsaved_marker_preserved=True,foreign_popup_guard_refused=True)
                # This tests a marker, not a business editor save/close contract.

            except BaseException as exc:evidence.update(status='failed',error_type=type(exc).__name__)
            finally:window.destroy()
        window.events.loaded+=loaded
        return window
    webview.create_window=observed_create
    # Hard fallback is only a process deadline, never a success path.
    threading.Timer(60,lambda:os._exit(97)).start()
    try:run_window(origin)
    finally:
        (root/'visible-window-result.json').write_text(json.dumps(evidence))
    if evidence.get('status')=='visible-login-rendered' and evidence.get('popup_same_origin_login_rendered'):
        if len(observed_popups)!=1 or any(not p.closed or (p.control is not None and not p.control.IsDisposed) for p in observed_popups):raise RuntimeError('owned popup close unconfirmed')
        evidence['popup_owner_close_confirmed']=True
        (root/'visible-window-result.json').write_text(json.dumps(evidence))
    if evidence.get('status')!='visible-login-rendered':raise RuntimeError('visible actual window failed')
    print('NATIVE_ACTUAL_VISIBLE_WINDOW_CLOSED_PASS',json.dumps(evidence),flush=True)
    os._exit(0)

if __name__=='__main__':run(pathlib.Path(sys.argv[1]))
