"""Native adapter logic simulations, not Windows API qualification."""
import types
import unittest
from native import Native
from slice import Result, Kind, Refused
import launcher
from unittest.mock import patch

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

if __name__=='__main__':unittest.main()
