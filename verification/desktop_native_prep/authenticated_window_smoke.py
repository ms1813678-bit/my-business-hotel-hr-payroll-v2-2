"""NEW authenticated visible fixture. Disposable CI only, no credentials in evidence."""
import json,os,pathlib,sys,threading,time

def run(root):
    if sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true':raise RuntimeError('disposable native CI only')
    sys.path.insert(0,str(root/'source'))
    import webview
    from app.desktop_window import run_window
    origin=json.loads((root/'desktop.json').read_text())['origin']
    seed=json.loads((root/'auth-seed.json').read_text())
    username=os.environ['FIXTURE_ADMIN_USERNAME'];password=os.environ['FIXTURE_ADMIN_PASSWORD']
    original=webview.create_window;evidence={};started=threading.Event()
    def observed_create(*args,**kwargs):
        window=original(*args,**kwargs)
        def js(code):return window.evaluate_js(code)
        def wait_for(expression):
            until=time.monotonic()+15
            while time.monotonic()<until:
                try:
                    if js(expression):return
                except Exception:pass
                time.sleep(.1)
            raise RuntimeError('authenticated DOM deadline')
        def capture(name):
            import win32gui,win32process
            from PIL import ImageGrab
            matches=[]
            def each(hwnd,_):
                _,pid=win32process.GetWindowThreadProcessId(hwnd)
                if pid==os.getpid() and win32gui.IsWindowVisible(hwnd) and win32gui.GetWindowText(hwnd)=='HOnY':matches.append(hwnd)
            win32gui.EnumWindows(each,None)
            if len(matches)!=1:raise RuntimeError('exact owned auth window ambiguous')
            win32gui.SetForegroundWindow(matches[0]);time.sleep(.4)
            image=ImageGrab.grab(bbox=win32gui.GetWindowRect(matches[0]),all_screens=True)
            image.save(root/name)
            return [image.width,image.height]
        def loaded():
            if started.is_set():return
            started.set()
            try:
                wait_for('location.pathname==="/login" && document.querySelectorAll("input[type=password]").length===1')
                # Normal DOM submission preserves actual session-bound CSRF. No cookie forging.
                js('document.querySelector("input[name=username]").value='+json.dumps(username)+';document.querySelector("input[name=password]").value='+json.dumps(password)+';document.querySelector("form.login-card button[type=submit]").click();true')
                wait_for('location.pathname==="/" && !document.querySelector("input[type=password]")')
                evidence['authenticated_dashboard_pixels']=capture('authenticated-dashboard.png')
                page='/payroll/runs/'+str(seed['payroll_run_id'])
                js('location.href='+json.dumps(origin+page)+';true')
                wait_for('location.pathname==='+json.dumps(page)+' && document.querySelectorAll("form.payroll-item-edit").length===1 && typeof payrollReviewSubmit==="function"')
                if not js('!!document.querySelector('+json.dumps('form[action="/payroll/items/'+str(seed['payroll_item_id'])+'/absence-leave-review"]')+')'):raise RuntimeError('real seeded conflict form absent')
                evidence['authenticated_payroll_pixels']=capture('authenticated-payroll.png')
                from urllib.parse import urlencode
                query=urlencode(dict(report_type='payroll',start_date=seed['report_start_date'],end_date=seed['report_end_date'],employee_id=seed['employee_id']))
                js('location.href='+json.dumps(origin+'/reports/view?'+query)+';true')
                wait_for('location.pathname==="/reports/view" && !!document.querySelector("a[href^=\'/reports/export.pdf?\']") && !!document.querySelector("button[onclick=\'window.print()\']")')
                evidence['authenticated_report_pixels']=capture('authenticated-report.png')
                evidence.update(status='authenticated-pages-rendered',normal_TLS=True,real_login_submission=True,pdf_download_tested=False,print_tested=False,dirty_cancel_tested=False,seeded_approvals_not_exercised=True)
            except BaseException as exc:evidence.update(status='failed',error_type=type(exc).__name__)
            finally:window.destroy()
        window.events.loaded+=loaded
        return window
    webview.create_window=observed_create
    threading.Timer(70,lambda:os._exit(97)).start()
    try:run_window(origin)
    finally:(root/'authenticated-window-result.json').write_text(json.dumps(evidence))
    if evidence.get('status')!='authenticated-pages-rendered':raise RuntimeError('authenticated actual pages failed')
    print('NATIVE_ACTUAL_AUTHENTICATED_PAGES_CLOSED_PASS',json.dumps(evidence),flush=True)
    os._exit(0)

if __name__=='__main__':run(pathlib.Path(sys.argv[1]))
