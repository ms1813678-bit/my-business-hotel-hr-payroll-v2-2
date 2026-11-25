import unittest,json
from transaction_model import *
H='a'*64;J='b'*64
class TransactionTests(unittest.TestCase):
 def record(self,k='release',id='owned-release',absent=True):return Owned(k,id,absent,H)
 def test_reverse_owned_only(self):
  r=[self.record(),self.record('service','owned-service')];s,p=plan(r,{(x.kind,x.identity):H for x in r});self.assertEqual([x.kind for x in s],['service','release']);self.assertEqual(p,[])
 def test_foreign_and_preexisting_preserved(self):
  r=[self.record(absent=False),self.record('service','other')];s,p=plan(r,{('release','owned-release'):H,('service','other'):J});self.assertEqual(s,[]);self.assertEqual(p,[(0,'preexisting'),(1,'identity_changed')])
 def test_already_absent(self):
  s,p=plan([self.record()],{});self.assertEqual(s,[]);self.assertEqual(p,[(0,'already_absent')])
 def test_bad_ownership_refuses(self):
  for r in ([self.record(),self.record()],[self.record('database')],[self.record('service'),self.record()], [Owned('release','x',1,H)], [Owned('release','x',True,'bad')]):
   with self.assertRaises(Refusal):plan(r,{})
 def test_independent_compensation(self):
  r=[self.record(),self.record('service','s'),self.record('shortcut','c')];steps,_=plan(r,{(x.kind,x.identity):H for x in r});called=[]
  def execute(s):
   called.append(s.kind)
   if s.kind=='shortcut':raise RuntimeError('synthetic failure')
   return 'compensated'
  out=compensate(steps,execute);self.assertEqual(called,['shortcut','service','release']);self.assertFalse(out['complete'])
 def test_replacement_fails_acceptance(self):
  r=[self.record()];s,_=plan(r,{('release','owned-release'):H});self.assertFalse(compensate(s,lambda _: 'preserved_identity_changed')['complete'])
 def test_service_failure_preserves_release_but_continues_others(self):
  r=[self.record(),self.record('shortcut','c'),self.record('service','s')];steps,_=plan(r,{(x.kind,x.identity):H for x in r});called=[]
  def execute(s):
   called.append(s.kind)
   if s.kind=='service':raise RuntimeError('synthetic live service')
   return 'compensated'
  out=compensate(steps,execute);self.assertEqual(called,['service','shortcut']);self.assertFalse(out['complete']);self.assertIn((0,'preserved_service_unconfirmed'),out['results'])
 def test_journal(self):
  d=dict(version=1,transaction='c'*32,phase='committing',records=[dict(kind='release',identity='owned',before_absent=True,after_fingerprint=H)])
  self.assertEqual(decode_journal(json.dumps(d).encode())[1],'committing')
  for raw in (b'{"version":1,"version":1}',json.dumps({**d,'version':True}).encode(),json.dumps({**d,'records':[dict(kind='database')]}).encode(),b' '*262145):
   with self.assertRaises(Refusal):decode_journal(raw)
if __name__=='__main__':unittest.main()
