"""Static design and expected-output model only. No native security evidence."""
import itertools,re,unittest
from pathlib import Path
S=Path(__file__).with_name('windows_policy_probe.c').read_text()
def accept(api,allowed,granted,index,lpac):
 return bool(api) and allowed==(index==0 or (index==1 and not lpac)) and granted==(0x120089 if allowed else 0)
class Contracts(unittest.TestCase):
 def test_sentinel(self):self.assertIn('!wcscmp(argv[1],L"--never-execute"))return 125',S)
 def test_sid_bounds(self):self.assertIn('sid_length<=end-sid',S)
 def test_file_mapping(self):self.assertIn('MapGenericMask(&desired,&mapping)',S);self.assertIn('FILE_GENERIC_READ',S)
 def test_no_error_flatten(self):self.assertNotIn('SetLastError(ERROR_ACCESS_DENIED)',S)
 def test_no_resume(self):self.assertNotRegex(S,r'\bResumeThread\s*\(')
 def test_self_only(self):self.assertIn('CreateProcessW(exe,cmd',S)
 def test_creation_suspend(self):self.assertIn('CREATE_SUSPENDED|CREATE_NO_WINDOW',S)
 def test_controls(self):self.assertIn('run_case(argc,argv,0)',S);self.assertIn('run_case(argc,argv,1)',S)
 def test_zero_capabilities(self):self.assertIn('groups->GroupCount!=0',S)
 def test_exact_job(self):self.assertIn('IsProcessInJob(process,job',S);self.assertIn('QueryInformationJobObject(job',S)
 def test_primary_duplicate_only_parent(self):self.assertIn('TOKEN_QUERY|TOKEN_DUPLICATE',S);self.assertIn('DuplicateToken(primary_token,SecurityImpersonation',S)
 def test_no_type_oracle(self):self.assertNotIn('TokenIsLessPrivilegedAppContainer',S)
 def test_no_policy_mutators(self):self.assertNotRegex(S,r'\b(?:SetNamedSecurityInfoW|SetSecurityInfo|RegSetValueExW|CreateAppContainerProfile|DeleteAppContainerProfile)\s*\(')
 def test_matrix(self):
  for lpac,index in itertools.product([False,True],range(3)):
   expected=index==0 or(index==1 and not lpac)
   self.assertTrue(accept(True,expected,(0x120089 if expected else 0),index,lpac))
   for api,allowed,granted in itertools.product([False,True],[False,True],[0,1,0x120089]):
    if not api or allowed!=expected or granted!=(0x120089 if allowed else 0):self.assertFalse(accept(api,allowed,granted,index,lpac))
 def test_all_denied_cannot_pass(self):
  self.assertFalse(all(accept(True,False,0,i,True)for i in range(3)))
 def test_same_as_ordinary_cannot_pass(self):
  self.assertFalse(all(accept(True,i!=2,(0x120089 if i!=2 else 0),i,True)for i in range(3)))
if __name__=='__main__':unittest.main(verbosity=2)
