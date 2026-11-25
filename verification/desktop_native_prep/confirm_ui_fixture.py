"""Owned synthetic-window UI Automation confirm cancellation, no custom-dialog override."""
import os,time,json

def cancel_confirm(hwnd,expected_message,output):
    import clr
    clr.AddReference('UIAutomationClient');clr.AddReference('UIAutomationTypes')
    from System import IntPtr
    from System.Windows.Automation import AutomationElement,TreeScope,Condition,ControlType,InvokePattern
    import win32gui,win32process
    from PIL import ImageGrab
    _,pid=win32process.GetWindowThreadProcessId(hwnd)
    if pid!=os.getpid() or win32gui.GetWindowText(hwnd)!='HOnY':raise RuntimeError('owned confirm root mismatch')
    root=AutomationElement.FromHandle(IntPtr(hwnd));until=time.monotonic()+8
    while time.monotonic()<until:
        elements=list(root.FindAll(TreeScope.Descendants,Condition.TrueCondition))
        names=[str(e.Current.Name) for e in elements]
        matches=[e for e in elements if e.Current.ControlType==ControlType.Button and str(e.Current.Name) in ('Cancel','إلغاء')]
        # Default browser UI must expose the exact page-provided warning and a
        # unique cancel button under THIS owned window before any invocation.
        if expected_message in names and len(matches)==1:
            ImageGrab.grab(bbox=win32gui.GetWindowRect(hwnd),all_screens=True).save(output)
            matches[0].GetCurrentPattern(InvokePattern.Pattern).Invoke()
            return
        time.sleep(.1)
    # Only structural counts and button labels from synthetic fixture are logged.
    print('NATIVE_CONFIRM_UIA_BOUNDARY',json.dumps(dict(elements=len(elements),exact_warning=expected_message in names,cancel_buttons=len(matches),button_names=[str(e.Current.Name) for e in elements if e.Current.ControlType==ControlType.Button]),ensure_ascii=True),flush=True)
    raise RuntimeError('actual default confirm identity unresolved')
