"""NEW visible native exact production window fixture, synthetic CI only."""
import json,os,pathlib,sys,time,threading

def run(root):
    if sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('disposable native CI only')
    sys.path.insert(0,str(root/'source'))
    import webview
    from app.desktop_window import run_window
    origin=json.loads((root/'desktop.json').read_text())['origin']
    original=webview.create_window
    evidence={}
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
    if evidence.get('status')!='visible-login-rendered':raise RuntimeError('visible actual window failed')
    print('NATIVE_ACTUAL_VISIBLE_WINDOW_CLOSED_PASS',json.dumps(evidence),flush=True)
    os._exit(0)

if __name__=='__main__':run(pathlib.Path(sys.argv[1]))
