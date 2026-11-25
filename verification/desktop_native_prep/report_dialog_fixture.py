"""Own synthetic report native dialogs, no callback replacement or physical printing."""
import os,time,pathlib,json,hashlib

def setup(hwnd):
    import clr,sys,win32gui,win32process
    framework='Framework64' if sys.maxsize>2**32 else 'Framework'
    for n in ('UIAutomationClient','UIAutomationTypes'):
        p=pathlib.Path(os.environ['SystemRoot'])/'Microsoft.NET'/framework/'v4.0.30319/WPF'/(n+'.dll')
        if not p.is_file():raise RuntimeError('UIA assembly absent')
        clr.AddReference(str(p))
    if win32process.GetWindowThreadProcessId(hwnd)[1]!=os.getpid() or win32gui.GetWindowText(hwnd)!='HOnY':raise RuntimeError('owned report root mismatch')

def save_pdf(hwnd,destination,shot):
    setup(hwnd)
    from System import IntPtr
    from System.Windows.Automation import AutomationElement,TreeScope,Condition,ControlType,InvokePattern,ValuePattern
    import win32gui,win32process
    from PIL import ImageGrab
    destination=pathlib.Path(destination)
    if destination.exists() or not destination.parent.is_dir():raise RuntimeError('fresh owned PDF path required')
    until=time.monotonic()+10;diagnostic={}
    while time.monotonic()<until:
        dialogs=[]
        def each(h,_):
            if win32process.GetWindowThreadProcessId(h)[1]!=os.getpid() or win32gui.GetClassName(h)!='#32770' or not win32gui.IsWindowVisible(h):return
            owner=h
            for _ in range(8):
                owner=win32gui.GetWindow(owner,4)
                if owner==hwnd:dialogs.append(h);return
                if not owner:return
        win32gui.EnumWindows(each,None)
        if len(dialogs)==1:
            h=dialogs[0];root=AutomationElement.FromHandle(IntPtr(h));es=list(root.FindAll(TreeScope.Descendants,Condition.TrueCondition))
            edits=[e for e in es if e.Current.ControlType==ControlType.Edit and str(e.Current.AutomationId)=='1001']
            saves=[e for e in es if e.Current.ControlType==ControlType.Button and str(e.Current.Name) in ('Save','حفظ')]
            diagnostic=dict(edits=[dict(name=str(e.Current.Name),id=str(e.Current.AutomationId)) for e in es if e.Current.ControlType==ControlType.Edit],buttons=[str(e.Current.Name) for e in es if e.Current.ControlType==ControlType.Button])
            if len(edits)==1 and len(saves)==1:
                # Test the shown default as-is, not the unqualified editor.
                shown=str(edits[0].GetCurrentPattern(ValuePattern.Pattern).Current.Value)
                actual=pathlib.Path(os.environ['USERPROFILE'])/'Downloads'/'MY_HR_Report_2026-03-01_2026-03-31.pdf'
                if shown not in (actual.name,actual.stem,str(actual)):
                    print('NATIVE_DEFAULT_PICKER_VALUE',json.dumps(dict(shown=shown,expected=str(actual))),flush=True)
                    raise RuntimeError('exact synthetic default filename mismatch')
                if actual.exists():raise RuntimeError('synthetic default download already exists')
                ImageGrab.grab(bbox=win32gui.GetWindowRect(h),all_screens=True).save(shot)
                saves[0].GetCurrentPattern(InvokePattern.Pattern).Invoke()
                break
        time.sleep(.1)
    else:
        print('NATIVE_REPORT_SAVE_BOUNDARY',json.dumps(diagnostic),flush=True)
        ImageGrab.grab(bbox=win32gui.GetWindowRect(hwnd),all_screens=True).save(shot)
        raise RuntimeError('exact native report Save dialog unresolved')
    until=time.monotonic()+15;last=None;stable=0
    while time.monotonic()<until:
        if actual.is_file():
            raw=actual.read_bytes()
            if raw==last:stable+=1
            else:stable=0
            if stable>=3 and raw.startswith(b'%PDF-') and b'%%EOF' in raw[-1024:]:
                destination.write_bytes(raw)
                if destination.read_bytes()!=raw:raise RuntimeError('owned PDF preservation mismatch')
                actual.unlink()
                if actual.exists():raise RuntimeError('exact owned default PDF cleanup unconfirmed')
                return dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),default_path_tested=True,chosen_path_tested=False,owned_default_removed=True)
            last=raw
        time.sleep(.2)
    print('NATIVE_REPORT_DOWNLOAD_BOUNDARY',json.dumps(dict(exists=destination.exists(),bytes=destination.stat().st_size if destination.exists() else None,siblings=[dict(name=p.name,bytes=p.stat().st_size) for p in destination.parent.glob('authenticated-report*') if p.is_file()])),flush=True)
    raise RuntimeError('native downloaded PDF incomplete')

def cancel_print(hwnd,shot):
    setup(hwnd)
    from System import IntPtr
    from System.Windows.Automation import AutomationElement,TreeScope,Condition,ControlType,InvokePattern
    import win32gui
    from PIL import ImageGrab
    root=AutomationElement.FromHandle(IntPtr(hwnd));until=time.monotonic()+12;diagnostic={}
    while time.monotonic()<until:
        es=list(root.FindAll(TreeScope.Descendants,Condition.TrueCondition));names=[str(e.Current.Name) for e in es]
        cancel=[e for e in es if e.Current.ControlType==ControlType.Button and str(e.Current.Name) in ('Cancel','إلغاء')]
        diagnostic=dict(print_text=any(n in ('Print','طباعة','Print preview') for n in names),cancel_buttons=len(cancel),buttons=[str(e.Current.Name) for e in es if e.Current.ControlType==ControlType.Button])
        if diagnostic['print_text'] and len(cancel)==1:
            # Bounded normal readiness observation only. No print invocation,
            # settings change, DOM replacement or alternate renderer.
            observed=time.monotonic()
            for seconds in (2,8,20):
                while time.monotonic()-observed<seconds:time.sleep(.1)
                current=list(root.FindAll(TreeScope.Descendants,Condition.TrueCondition))
                current_cancel=[e for e in current if e.Current.ControlType==ControlType.Button and str(e.Current.Name) in ('Cancel','إلغاء')]
                if len(current_cancel)!=1:raise RuntimeError('owned preview Cancel changed while waiting')
                ImageGrab.grab(bbox=win32gui.GetWindowRect(hwnd),all_screens=True).save(pathlib.Path(shot).with_name('authenticated-print-ready-'+str(seconds)+'.png'))
                print('NATIVE_PRINT_READINESS_OBSERVATION',json.dumps(dict(seconds=seconds,cancel_buttons=len(current_cancel),names=[str(e.Current.Name)[:120] for e in current][:100])),flush=True)
            ImageGrab.grab(bbox=win32gui.GetWindowRect(hwnd),all_screens=True).save(shot)
            cancel=current_cancel
            cancel[0].GetCurrentPattern(InvokePattern.Pattern).Invoke()
            until_closed=time.monotonic()+6
            while time.monotonic()<until_closed:
                current=list(root.FindAll(TreeScope.Descendants,Condition.TrueCondition))
                if not any(e.Current.ControlType==ControlType.Button and str(e.Current.Name) in ('Cancel','إلغاء') for e in current):return
                time.sleep(.1)
            raise RuntimeError('print preview close unconfirmed')
        time.sleep(.1)
    print('NATIVE_REPORT_PRINT_BOUNDARY',json.dumps(diagnostic),flush=True)
    ImageGrab.grab(bbox=win32gui.GetWindowRect(hwnd),all_screens=True).save(shot)
    raise RuntimeError('exact native print preview Cancel unresolved')
