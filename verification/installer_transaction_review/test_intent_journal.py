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
