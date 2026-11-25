"""Source/canonical-identity failure cases without COM. Native tests remain required."""
import importlib.util,pathlib,sys,types
p=pathlib.Path(__file__).with_name('desktop_webview_guard.CANDIDATE.py')
s=importlib.util.spec_from_file_location('candidate_guard_test',p);g=importlib.util.module_from_spec(s);sys.modules[s.name]=g;s.loader.exec_module(g)
class Flags:Instance=1;NonPublic=2
released=[]
class Marshal:
    IsComObject=staticmethod(lambda x:isinstance(x,int))
    GetIUnknownForObject=staticmethod(lambda x:x)
    Release=staticmethod(lambda x:released.append(x))
class Kind:
    FullName='Microsoft.Web.WebView2.Core.CoreWebView2'
    Assembly=types.SimpleNamespace(GetName=lambda:types.SimpleNamespace(Version='1.0.2957.106'))
    def GetField(self,*args):return types.SimpleNamespace(FieldType=types.SimpleNamespace(FullName='System.Object'),GetValue=lambda obj:obj.raw)
class Value:
    def __init__(self,raw):self.raw=raw
    def GetType(self):return Kind()
sys.modules['System']=types.SimpleNamespace(IntPtr=types.SimpleNamespace(Zero=0))
sys.modules['System.Reflection']=types.SimpleNamespace(BindingFlags=Flags)
sys.modules['System.Runtime.InteropServices']=types.SimpleNamespace(Marshal=Marshal)
assert g._same_native_webview(Value(7),Value(7));assert released==[7,7];released.clear()
assert not g._same_native_webview(Value(7),Value(8));assert released==[8,7];released.clear()
for left,right in [(None,Value(7)),(Value(7),Value('not COM')),(Value(0),Value(7))]:
    try:g._same_native_webview(left,right)
    except g.GuardInstallError:pass
    else:raise AssertionError('unknown identity accepted')
Kind.FullName='unknown SDK type'
try:g._same_native_webview(Value(7),Value(7))
except g.GuardInstallError:pass
else:raise AssertionError('unknown type accepted')
print('CANDIDATE_IDENTITY_SOURCE_REFUSAL_REFERENCE_BALANCE_PASS')
