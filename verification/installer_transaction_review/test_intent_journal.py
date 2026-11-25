import unittest,tempfile,pathlib,json
from intent_journal import *
A=dict(kind='release',identity_digest='a'*64,before_absent=True,after_digest='b'*64)
class JournalTests(unittest.TestCase):
 def test_intent_before_effect_recovery(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'journal'
   with Journal(p) as j:j.append(0,'intent',A)
   self.assertEqual(read(p)['states'][0][0],'intent')
 def test_sequence(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'journal'
   with Journal(p) as j:
    for phase in ('intent','verified','compensate_intent','compensated'):j.append(0,phase,A)
   self.assertEqual(read(p)['records'],4)
 def test_refuse_existing_and_changed_action(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'journal'
   with Journal(p) as j:
    j.append(0,'intent',A)
    with self.assertRaises(FileExistsError):Journal(p)
    with self.assertRaises(Refusal):j.append(0,'verified',{**A,'after_digest':'c'*64})
    with self.assertRaises(Refusal):j.append(1,'verified',A)
   self.assertEqual(read(p)['records'],1)
 def test_torn_tampered_reordered_refuse(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'journal'
   with Journal(p) as j:
    j.append(0,'intent',A);j.append(0,'verified',A)
   raw=p.read_bytes();lines=raw.splitlines(keepends=True)
   for variant in (raw[:-1],raw.replace(b'release',b'service'),b''.join(reversed(lines)),raw+lines[-1]):
    p.write_bytes(variant)
    with self.assertRaises(Refusal):read(p)
 def test_terminal_and_retry(self):
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'journal'
   with Journal(p) as j:
    for phase in ('intent','failed','compensate_intent','compensated'):j.append(0,phase,A)
    with self.assertRaises(Refusal):j.append(0,'compensate_intent',A)
 def test_short_write_recovery(self):
  from unittest.mock import patch
  import intent_journal
  with tempfile.TemporaryDirectory() as t:
   p=Path(t)/'journal';real=intent_journal.os.write
   with Journal(p) as j:
    with patch('intent_journal.os.write',side_effect=lambda fd,b:real(fd,b[:13])):j.append(0,'intent',A)
   self.assertEqual(read(p)['records'],1)
if __name__=='__main__':unittest.main()

class CrashRecoveryTests(unittest.TestCase):
 def crash(self,folder,mode):
  import subprocess,sys,os
  child=Path(folder)/'child.py';journal=Path(folder)/'journal'
  # Fixed synthetic child. No external input executes as code.
  child.write_text('''import os,sys\nfrom pathlib import Path\nsys.path.insert(0,sys.argv[1])\nfrom intent_journal import Journal\na=dict(kind='release',identity_digest='a'*64,before_absent=True,after_digest='b'*64)\np=Path(sys.argv[2])\nj=Journal(p)\nj.append(0,'intent',a)\nif sys.argv[3]=='torn':\n os.write(j.fd,b'{\"unfinished\":')\n os.fsync(j.fd)\nos._exit(91)\n''')
  result=subprocess.run([sys.executable,'-I',str(child),str(Path(__file__).parent.resolve()),str(journal),mode],capture_output=True,timeout=10)
  self.assertEqual(result.returncode,91)
  return journal
 def test_abrupt_exit_after_fsync_retains_intent_only(self):
  with tempfile.TemporaryDirectory() as t:
   journal=self.crash(t,'flushed');result=read(journal)
   self.assertEqual(result['records'],1);self.assertEqual(result['states'][0][0],'intent')
   # Intent is ambiguity, never proof the external effect happened.
   self.assertNotEqual(result['states'][0][0],'verified')
 def test_abrupt_exit_torn_tail_refuses_whole_log(self):
  with tempfile.TemporaryDirectory() as t:
   journal=self.crash(t,'torn')
   with self.assertRaises(Refusal):read(journal)
