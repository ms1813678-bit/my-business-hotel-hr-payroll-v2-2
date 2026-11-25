"""Native adapter logic simulations, not Windows API qualification."""
import types
import unittest
from native import Native
from slice import Result, Kind, Refused
import launcher
from unittest.mock import patch, Mock

class WinError(Exception):
    def __init__(self, code): self.winerror = code

class Handle:
    def __init__(self): self.closed=0
    def Close(self): self.closed += 1

class NativeTests(unittest.TestCase):
    def n(self):
        n=object.__new__(Native);n.records={};n.root_pinned=False;n.journal_token=None
        n.types=types.SimpleNamespace(error=WinError);return n
    def test_tokens_bound_to_native_instance_and_operation(self):
        a,b=self.n(),self.n();t=a.register('root',Handle())
        self.assertTrue(a.issued(t,'root'));self.assertFalse(a.issued(t,'service'))
        self.assertFalse(b.issued(t,'root'))
        with self.assertRaises(Refused):b.record(t)
    def test_consumed_record_cannot_authorize_effect(self):
        n=self.n();t=n.register('service',Handle());n.records[t]['closed']=True
        with self.assertRaises(Refused):n.record(t)
    def test_create_root_collision_is_no_effect(self):
        n=self.n();n.validate_parent=lambda:None;n.root='fixed';n.attributes=lambda:None
        n.f=types.SimpleNamespace(CreateDirectory=lambda *args:(_ for _ in ()).throw(WinError(183)))
        self.assertIs(n.create_root().kind,Kind.NO_EFFECT);self.assertEqual(n.records,{})
    def test_root_open_failure_is_unknown_not_path_cleanup(self):
        n=self.n();n.validate_parent=lambda:None;n.root='fixed';n.attributes=lambda:None
        n.f=types.SimpleNamespace(CreateDirectory=lambda *args:None)
        n.hold=lambda *args,**kwargs:(_ for _ in ()).throw(WinError(5))
        self.assertIs(n.create_root().kind,Kind.UNKNOWN);self.assertEqual(n.records,{})
    def test_root_verify_failure_retains_owned_handle(self):
        n=self.n();n.validate_parent=lambda:None;n.root='fixed';n.attributes=lambda:None
        n.f=types.SimpleNamespace(CreateDirectory=lambda *args:None);h=Handle();n.hold=lambda *a,**k:h
        n.identity=lambda *a:(_ for _ in ()).throw(Refused('identity'))
        r=n.create_root();self.assertIs(r.kind,Kind.OWNED);self.assertIs(n.records[r.token]['handle'],h)
        self.assertIsNone(n.records[r.token]['identity'])
    def test_journal_owned_failure_retained_and_pins_root(self):
        n=self.n();t=object();n.create_file=lambda *a:Result(Kind.OWNED,t,'verify failed')
        with self.assertRaises(Refused):n.journal(object(),[])
        self.assertIs(n.journal_token,t);self.assertTrue(n.root_pinned)
    def test_journal_unknown_pins_root(self):
        n=self.n();n.create_file=lambda *a:Result(Kind.UNKNOWN,detail='ambiguous')
        with self.assertRaises(Refused):n.journal(object(),[])
        self.assertTrue(n.root_pinned)
    def test_close_fault_attempted_once(self):
        n=self.n();n.ancestors=[];n.scm=None
        h=Handle()
        def fail():h.closed+=1;raise WinError(6)
        h.Close=fail;t=n.register('root',h)
        with self.assertRaises(Refused):n.close()
        n.close();self.assertEqual(h.closed,1);self.assertTrue(n.records[t]['closed'])
    def test_launcher_failure_still_rollback_and_close(self):
        calls=[]
        class N:
            elevated_context_restored=True
            def __init__(self,*a):pass
            def close(self):calls.append('close')
            def final_absence(self):return True
        class F:
            done=True
            def __init__(self,*a):pass
            def run(self):raise Refused('injected')
            def rollback(self):calls.append('rollback');return []
        with patch.object(launcher.sys,'platform','win32'),patch.dict(launcher.os.environ,{'GITHUB_ACTIONS':'true'}),patch.object(launcher.sys,'argv',['launcher.py']),patch.object(launcher,'Native',N),patch.object(launcher,'Slice',F):
            self.assertEqual(launcher.main(),1)
        self.assertEqual(calls,['rollback','close'])
    def test_launcher_context_failure_does_not_attempt_mutation_cleanup(self):
        calls=[]
        class N:
            elevated_context_restored=False
            def __init__(self,*a):pass
            def close(self):calls.append('close')
            def final_absence(self):return True
        class F:
            done=False
            def __init__(self,*a):pass
            def run(self):raise Refused('injected')
            def rollback(self):calls.append('rollback');return []
        with patch.object(launcher.sys,'platform','win32'),patch.dict(launcher.os.environ,{'GITHUB_ACTIONS':'true'}),patch.object(launcher.sys,'argv',['launcher.py']),patch.object(launcher,'Native',N),patch.object(launcher,'Slice',F):
            self.assertEqual(launcher.main(),1)
        self.assertEqual(calls,['close'])

class AbsenceTests(unittest.TestCase):
    def test_lstat_missing_present_and_permission_unknown(self):
        with patch('native.os.lstat',side_effect=FileNotFoundError):
            self.assertTrue(Native.path_absent('fixed'))
        with patch('native.os.lstat',return_value=object()):
            self.assertFalse(Native.path_absent('fixed'))
        with patch('native.os.lstat',side_effect=PermissionError):
            with self.assertRaises(PermissionError):Native.path_absent('fixed')
        with patch('native.os.lstat',side_effect=OSError):
            with self.assertRaises(OSError):Native.path_absent('fixed')
    def fake(self):
        n=object.__new__(Native);n.root='fixed';n.name='fixed'
        n.ws=types.SimpleNamespace(SC_MANAGER_CONNECT=1,SERVICE_QUERY_STATUS=4,
            OpenSCManager=Mock(return_value=Handle()),OpenService=Mock(side_effect=WinError(1060)),CloseServiceHandle=Mock())
        n.types=types.SimpleNamespace(error=WinError);return n
    def test_final_absence_checks_both_resources(self):
        n=self.fake()
        with patch('native.os.lstat',side_effect=FileNotFoundError):self.assertTrue(n.final_absence())
        n.ws.OpenService.assert_called_once()
    def test_final_absence_present_or_unknown_never_pass(self):
        n=self.fake()
        with patch('native.os.lstat',return_value=object()):
            with self.assertRaises(Refused):n.final_absence()
        with patch('native.os.lstat',side_effect=PermissionError):
            with self.assertRaises(PermissionError):n.final_absence()
    def test_remove_postclose_uses_same_exact_absence_semantics(self):
        for effect,expected in ((FileNotFoundError,None),(None,Refused),(PermissionError,PermissionError)):
            n=object.__new__(Native);h=Handle();token=object()
            n.records={token:dict(operation='payload.txt',handle=h,path='fixed',directory=False,
                closed=False,absent=False,identity='owned')}
            n.record=lambda t:n.records[t];n.f=types.SimpleNamespace(SetFileInformationByHandle=Mock(),FileDispositionInfo=4)
            with patch('native.os.lstat',side_effect=effect,return_value=object()):
                if expected:
                    with self.assertRaises(expected):n.remove(token)
                    self.assertFalse(n.records[token]['absent'])
                else:n.remove(token);self.assertTrue(n.records[token]['absent'])
            self.assertEqual(h.closed,1)
    def test_reparse_flag_from_win32file_not_win32con(self):
        n=object.__new__(Native)
        n.c=types.SimpleNamespace(GENERIC_READ=1,READ_CONTROL=2,FILE_SHARE_READ=1,OPEN_EXISTING=3,FILE_FLAG_BACKUP_SEMANTICS=32)
        n.f=types.SimpleNamespace(FILE_FLAG_OPEN_REPARSE_POINT=64,CreateFile=Mock(return_value=Handle()))
        n.hold('fixed',True)
        self.assertEqual(n.f.CreateFile.call_args.args[5],96)

if __name__=='__main__':unittest.main()
