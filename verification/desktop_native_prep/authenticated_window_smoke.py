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
    from app.desktop_webview_guard import WebView2OriginGuard
    guards=[];original_init=WebView2OriginGuard.__init__
    def observed_init(self,*args,**kwargs):
        original_init(self,*args,**kwargs);guards.append(self)
    WebView2OriginGuard.__init__=observed_init
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
            # At this runner's 1024px client viewport CSS intentionally stacks
            # navigation before main. Scroll real content into view, never hide
            # or restyle it for proof; preserve geometry as a layout caveat.
            geometry=js('JSON.stringify((function(){var m=document.querySelector("main.content");if(!m)throw new Error("main absent");m.scrollIntoView({block:"start",behavior:"instant"});return {viewport:innerWidth,mainTop:m.getBoundingClientRect().top,scrollY:scrollY,sidebarHeight:document.querySelector("aside.sidebar").getBoundingClientRect().height};})())')
            evidence[name+'_geometry']=json.loads(geometry) if isinstance(geometry,str) else geometry
            time.sleep(.3)
            if not js('document.querySelector("main.content h1").getBoundingClientRect().top>=0 && document.querySelector("main.content h1").getBoundingClientRect().bottom<innerHeight'):raise RuntimeError('actual main heading not visible')
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
        def cancel_dirty_review():
            guard=next(g for g in guards if g._window is window)
            selector='form.payroll-item-edit[action="/payroll/items/'+str(seed['payroll_item_id'])+'/update"] input[name=other_bonus]'
            review='form[action="/payroll/items/'+str(seed['payroll_item_id'])+'/absence-leave-review"] button[type=submit]'
            expected=js('document.getElementById("review-unsaved-warning").textContent')
            if not isinstance(expected,str) or not expected.strip():raise RuntimeError('exact dirty warning absent')
            # Dispatch a real input event; do not replace confirm or submit logic.
            js('var input=document.querySelector('+json.dumps(selector)+');input.value="7";input.dispatchEvent(new Event("input",{bubbles:true}));true')
            wait_for('document.querySelector("form.payroll-item-edit").dataset.dirty==="1"')
            state=[]
            def fire():
                core=guard._control.CoreWebView2
                state.append(core.ExecuteScriptAsync('window.__fixtureReviewAttemptDone=false;window.__fixtureReviewPrevented=null;document.addEventListener("submit",function(e){window.__fixtureReviewPrevented=e.defaultPrevented;},{once:true});document.querySelector('+json.dumps(review)+').scrollIntoView({block:"center"});setTimeout(function(){document.querySelector('+json.dumps(review)+').click();window.__fixtureReviewAttemptDone=true;},0);true'))
            guard._marshal_to_ui(guard._control,fire)
            import win32gui,win32process
            owned=[]
            win32gui.EnumWindows(lambda h,_:owned.append(h) if win32process.GetWindowThreadProcessId(h)[1]==os.getpid() and win32gui.GetWindowText(h)=='HOnY' else None,None)
            if len(owned)!=1:raise RuntimeError('exact dirty parent identity ambiguous')
            import importlib.util
            spec=importlib.util.spec_from_file_location('owned_confirm_fixture',pathlib.Path(__file__).with_name('confirm_ui_fixture.py'))
            helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
            helper.cancel_confirm(owned[0],expected,root/'authenticated-dirty-confirm.png')
            wait_for('window.__fixtureReviewAttemptDone===true && window.__fixtureReviewPrevented===true')
            if not js('document.querySelector('+json.dumps(selector)+').value==="7" && document.querySelector("form.payroll-item-edit").dataset.dirty==="1"'):raise RuntimeError('dirty cancel lost unsaved value')
            evidence['dirty_cancel_tested']=True
        def report_output_gate():
            import win32gui,win32process,importlib.util
            guard=next(g for g in guards if g._window is window)
            owned=[]
            win32gui.EnumWindows(lambda h,_:owned.append(h) if win32process.GetWindowThreadProcessId(h)[1]==os.getpid() and win32gui.GetWindowText(h)=='HOnY' else None,None)
            if len(owned)!=1:raise RuntimeError('exact report parent identity ambiguous')
            spec=importlib.util.spec_from_file_location('owned_report_dialog',pathlib.Path(__file__).with_name('report_dialog_fixture.py'))
            helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
            # Actual page link, existing backend DownloadStarting + SaveFileDialog.
            # Do not replace callbacks, cookies, PDF endpoint, print function or UI.
            def click(selector):
                def fire():
                    guard._control.CoreWebView2.ExecuteScriptAsync('setTimeout(function(){document.querySelector('+json.dumps(selector)+').click();},0);true')
                guard._marshal_to_ui(guard._control,fire)
            click('a[href^="/reports/export.pdf?"]')
            evidence['native_pdf']=helper.save_pdf(owned[0],root/'authenticated-report.pdf',root/'authenticated-save-dialog.png')
            evidence['pdf_download_tested']=True
            wait_for('location.pathname==="/reports/view"')
            click('button[onclick="window.print()"]')
            helper.cancel_print(owned[0],root/'authenticated-print-preview.png')
            wait_for('location.pathname==="/reports/view"')
            evidence['print_preview_cancel_tested']=True
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
                cancel_dirty_review()
                from urllib.parse import urlencode
                query=urlencode(dict(report_type='payroll',start_date=seed['report_start_date'],end_date=seed['report_end_date'],employee_id=seed['employee_id']))
                js('location.href='+json.dumps(origin+'/reports/view?'+query)+';true')
                wait_for('location.pathname==="/reports/view" && !!document.querySelector("a[href^=\'/reports/export.pdf?\']") && !!document.querySelector("button[onclick=\'window.print()\']")')
                evidence['authenticated_report_pixels']=capture('authenticated-report.png')
                report_output_gate()
                evidence.update(status='authenticated-pages-rendered',normal_TLS=True,real_login_submission=True,pdf_download_tested=evidence.get('pdf_download_tested',False),print_tested=False,print_preview_cancel_tested=evidence.get('print_preview_cancel_tested',False),dirty_cancel_tested=evidence.get('dirty_cancel_tested',False),seeded_approvals_not_exercised=True)
            except BaseException as exc:
                evidence.update(status='failed',error_type=type(exc).__name__)
                print('NATIVE_AUTH_FIXTURE_FAILED',type(exc).__name__,'dirty_cancel',evidence.get('dirty_cancel_tested',False),flush=True)
            finally:window.destroy()
        window.events.loaded+=loaded
        return window
    webview.create_window=observed_create
    threading.Timer(110,lambda:os._exit(97)).start()
    try:run_window(origin)
    finally:(root/'authenticated-window-result.json').write_text(json.dumps(evidence))
    if evidence.get('status')!='authenticated-pages-rendered':raise RuntimeError('authenticated actual pages failed')
    print('NATIVE_ACTUAL_AUTHENTICATED_PAGES_CLOSED_PASS',json.dumps(evidence),flush=True)
    os._exit(0)

if __name__=='__main__':run(pathlib.Path(sys.argv[1]))
