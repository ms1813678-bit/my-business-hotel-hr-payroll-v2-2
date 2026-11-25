import unittest,copy
from identity_output import *
from test_correlate import fixtures
class IdentityTests(unittest.TestCase):
 def test_exact(self):
  r,i=fixtures();r[0]['data']['ImageName']='C:\\owned\\python.exe';r[0]['data']['CreateTime']=1
  self.assertEqual(bind(r,1,10,'C:\\owned\\python.exe',0,10)['created'],1)
  self.assertEqual(child_id(b'CANDIDATE_CREATED pid=10 suspended=1\n'),10)
 def test_refuse_identity(self):
  r,i=fixtures()
  for parent,pid,image,start,end in ((2,10,'C:/owned/python.exe',0,10),(1,11,'C:/owned/python.exe',0,10),(1,10,'python.exe',0,10),(1,10,'C:/other/python.exe',0,10),(1,10,'C:/owned/python.exe',2,10)):
   with self.assertRaises(Refusal):bind(r,parent,pid,image,start,end)
  for b in (b'',b'CANDIDATE_CREATED pid=0 suspended=1\n',b'CANDIDATE_CREATED pid=10 suspended=1\n'*2):
   with self.assertRaises(Refusal):child_id(b)
 def test_sanitized(self):
  raw=dict(exit_code=0xc0000022,operations=[dict(path='C:\\runner-private\\opaque.dll',status=0xc0000022),dict(path='C:\\owned\\synthetic.xls',status=0)])
  out=sanitized(raw,b'k'*32,{'synthetic.xls'})
  self.assertNotIn('runner-private',str(out));self.assertNotIn('opaque.dll',str(out));self.assertEqual(out['operations'][1]['synthetic_basename'],'synthetic.xls')
  self.assertFalse(out['cause_established'])
if __name__=='__main__':unittest.main()
