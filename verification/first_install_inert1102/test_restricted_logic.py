"""Simulated API contracts only, no Windows authorization evidence."""
import types,unittest
from pathlib import PureWindowsPath
from unittest.mock import Mock
from native import Native
from test_native_logic import Handle,WinError
from slice import Refused
import restricted_checks

class RestrictedTests(unittest.TestCase):
    def fixture(self):
        constants=dict(TOKEN_DUPLICATE=2,TOKEN_QUERY=8,TOKEN_IMPERSONATE=4,WinBuiltinAdministratorsSid=26,
            WinLocalSystemSid=22,DISABLE_MAX_PRIVILEGE=1,SecurityImpersonation=2,TokenImpersonation=2)
        s=types.SimpleNamespace(**constants);s.OpenProcessToken=Mock(return_value=Handle())
        s.CreateWellKnownSid=lambda a,b:a;s.CreateRestrictedToken=Mock(return_value=Handle())
        s.DuplicateTokenEx=Mock(return_value=Handle());state={'reduced':False}
        s.CheckTokenMembership=lambda token,sid: False if token is not None else not state['reduced']
        s.ImpersonateLoggedOnUser=Mock(side_effect=lambda token:state.update(reduced=True))
        s.RevertToSelf=Mock(side_effect=lambda:state.update(reduced=False))
        c=types.SimpleNamespace(DELETE=65536,WRITE_DAC=262144,GENERIC_WRITE=0x40000000,GENERIC_READ=0x80000000,
            READ_CONTROL=131072,FILE_SHARE_READ=1,FILE_SHARE_WRITE=2,FILE_SHARE_DELETE=4,OPEN_EXISTING=3,
            FILE_FLAG_OPEN_REPARSE_POINT=0x200000,FILE_FLAG_BACKUP_SEMANTICS=0x2000000)
        parent=PureWindowsPath('C:/Program Files');read=Handle()
        def open_file(path,*args):
            if path==str(parent):return read
            raise WinError(5)
        f=types.SimpleNamespace(CreateFile=Mock(side_effect=open_file),FILE_FLAG_OPEN_REPARSE_POINT=0x200000)
        ws=types.SimpleNamespace(SC_MANAGER_CONNECT=1,SERVICE_CHANGE_CONFIG=2,SERVICE_START=16,
            OpenSCManager=Mock(return_value=Handle()),OpenService=Mock(side_effect=WinError(5)),CloseServiceHandle=Mock())
        n=types.SimpleNamespace(sec=s,c=c,f=f,ws=ws,parent=parent,root=parent/'fixture',name='fixture',
            api=types.SimpleNamespace(GetCurrentProcess=lambda:1),types=types.SimpleNamespace(error=WinError),
            identity=Mock(),elevated_context_restored=True)
        return n,state,read
    def test_call_shape_positive_and_nine_negative_checks(self):
        n,state,read=self.fixture();out=restricted_checks.check(n)
        n.sec.DuplicateTokenEx.assert_called_once_with(n.sec.CreateRestrictedToken.return_value,2,12,2,None)
        self.assertEqual(len(out),10);self.assertEqual(out[0],'trusted-parent-read:granted')
        self.assertTrue(n.elevated_context_restored);self.assertFalse(state['reduced']);self.assertEqual(read.closed,1)
    def test_impersonation_call_failure_checks_restoration(self):
        n,state,read=self.fixture();n.sec.ImpersonateLoggedOnUser.side_effect=WinError(5)
        with self.assertRaises(WinError):restricted_checks.check(n)
        self.assertTrue(n.elevated_context_restored);n.sec.RevertToSelf.assert_not_called()
        n.f.CreateFile.assert_not_called()
    def test_positive_failure_cannot_count_as_denials(self):
        n,state,read=self.fixture();n.f.CreateFile.side_effect=WinError(5)
        with self.assertRaises(WinError):restricted_checks.check(n)
        self.assertTrue(n.elevated_context_restored);n.ws.OpenService.assert_not_called()
    def test_sharing_failure_not_acl_denial(self):
        n,state,read=self.fixture();n.f.CreateFile.side_effect=[read,WinError(32)]
        with self.assertRaises(Refused):restricted_checks.check(n)
        self.assertTrue(n.elevated_context_restored)
    def test_file_descriptor_explicit_no_inheritance(self):
        n=object.__new__(Native);n.types=types.SimpleNamespace(SECURITY_ATTRIBUTES=lambda:types.SimpleNamespace())
        convert=Mock(return_value='file-descriptor');n.sec=types.SimpleNamespace(ConvertStringSecurityDescriptorToSecurityDescriptor=convert,SDDL_REVISION_1=1)
        sa=n.attributes(directory=False);self.assertEqual(sa.SECURITY_DESCRIPTOR,'file-descriptor')
        self.assertEqual(convert.call_args.args[0],'O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)')
        self.assertFalse(sa.bInheritHandle)

    def security_native(self,flags):
        n=object.__new__(Native)
        class SD:
            def GetSecurityDescriptorOwner(self):return 'S-1-5-32-544'
            def GetSecurityDescriptorControl(self):return 4096,1
            def GetSecurityDescriptorDacl(self):
                aces=[((0,flags),0x1F01FF,sid) for sid in ('S-1-5-18','S-1-5-32-544')]
                return types.SimpleNamespace(GetAceCount=lambda:2,GetAce=lambda i:aces[i])
        n.c=types.SimpleNamespace(GENERIC_ALL=0x10000000,INHERIT_ONLY_ACE=8)
        n.sec=types.SimpleNamespace(GetSecurityInfo=lambda *a:SD(),SE_FILE_OBJECT=1,OWNER_SECURITY_INFORMATION=1,
            DACL_SECURITY_INFORMATION=4,ConvertSidToStringSid=lambda x:x,ACCESS_ALLOWED_ACE_TYPE=0,SE_DACL_PROTECTED=4096)
        return n
    def test_file_and_directory_exact_flags_never_interchangeable(self):
        n=self.security_native(0);n.acl(None,exact=True,directory=False)
        with self.assertRaises(Refused):n.acl(None,exact=True,directory=True)
        n=self.security_native(3);n.acl(None,exact=True,directory=True)
        with self.assertRaises(Refused):n.acl(None,exact=True,directory=False)
    def test_service_dependency_and_other_auxiliary_config_refused(self):
        n=self.security_native(0);sd=n.sec.GetSecurityInfo()
        # Service uses its distinct full-access mask, not file full access.
        n.ws=types.SimpleNamespace(SERVICE_ALL_ACCESS=0x1F01FF,SERVICE_WIN32_OWN_PROCESS=16,
            SERVICE_DISABLED=4,SERVICE_ERROR_NORMAL=1,SERVICE_STOPPED=1,
            QueryServiceStatusEx=lambda h:dict(CurrentState=1,ProcessId=0),QueryServiceObjectSecurity=lambda *a:sd)
        n.record=lambda t:{'handle':None};n.name='fixture';n.command='absent.exe'
        good=[16,4,1,'absent.exe','',0,[],'NT AUTHORITY\\NetworkService','fixture']
        n.ws.QueryServiceConfig=lambda h:good
        n.verify_service(None)
        for index,value in ((2,2),(4,'group'),(5,1),(6,['unexpected']),(8,'foreign')):
            bad=list(good);bad[index]=value;n.ws.QueryServiceConfig=lambda h,bad=bad:bad
            with self.assertRaises(Refused):n.verify_service(None)

if __name__=='__main__':unittest.main()
