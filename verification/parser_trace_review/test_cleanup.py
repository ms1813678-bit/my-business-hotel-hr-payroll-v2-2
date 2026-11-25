import unittest,tempfile,pathlib,subprocess,json
from watchdog import stop_all,remove_raw,NAMES,RAW
class CleanupTests(unittest.TestCase):
 def test_first_timeout_does_not_skip_other(self):
  calls=[]
  def fake(args,**kw):
   calls.append(args)
   if args[1]=='stop-path' and args[2]==NAMES[0]:raise subprocess.TimeoutExpired(args,3)
   return subprocess.CompletedProcess(args,0,b'{"absent":true}',b'')
  with tempfile.TemporaryDirectory() as t:
   out=stop_all('probe',pathlib.Path(t),fake)
   self.assertEqual(len(calls),4);self.assertTrue(all(v['absent'] for v in out.values()))
 def test_first_bad_query_does_not_skip_other(self):
  calls=[]
  def fake(args,**kw):
   calls.append(args)
   return subprocess.CompletedProcess(args,0,b'bad' if args[2]==NAMES[0] else b'{"absent":true}',b'')
  with tempfile.TemporaryDirectory() as t:
   out=stop_all('probe',pathlib.Path(t),fake)
   self.assertEqual(len(calls),4);self.assertFalse(out[NAMES[0]]['absent']);self.assertTrue(out[NAMES[1]]['absent'])
 def test_raw_cleanup_independent_and_preserves_unknown(self):
  with tempfile.TemporaryDirectory() as t:
   root=pathlib.Path(t)
   for name in RAW:(root/name).write_bytes(b'synthetic')
   (root/'unrelated').write_bytes(b'preserve')
   self.assertTrue(remove_raw(root));self.assertTrue((root/'unrelated').is_file())
   (root/RAW[0]).mkdir();(root/RAW[1]).write_bytes(b'synthetic')
   self.assertFalse(remove_raw(root));self.assertFalse((root/RAW[1]).exists());self.assertTrue((root/RAW[0]).is_dir())
if __name__=='__main__':unittest.main()
