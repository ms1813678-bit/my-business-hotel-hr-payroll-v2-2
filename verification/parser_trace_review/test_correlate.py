import unittest,copy
from correlate import *
def fixtures():
 def r(provider,id,ts,data,version=0):return dict(provider=provider,id=id,ts=ts,data=data,version=version)
 identity=dict(pid=10,ppid=1,created=100,image='C:/owned/python.exe',launcher_started=0,launcher_ended=10)
 rows=[r(PROCESS,1,1,dict(ProcessID=10,ParentProcessID=1,CreateTime=100,ImageName='C:/owned/python.exe')),r(PROCESS,3,2,dict(ProcessID=10,ThreadID=20)),r(FILE,12,3,dict(Irp=99,IssuingThreadId=20,FileName='C:/owned/missing.dll'),1),r(FILE,24,4,dict(Irp=99,Status=0xc0000022)),r(PROCESS,4,5,dict(ProcessID=10,ThreadID=20)),r(PROCESS,2,6,dict(ProcessID=10,CreateTime=100,ExitCode=0xc0000022))]
 return rows,identity
class Tests(unittest.TestCase):
 def test_owned(self):
  r,i=fixtures();self.assertEqual(correlate(r,i,0)['operations'][0]['status'],0xc0000022)
 def test_refusals(self):
  cases=['loss','unknownloss','startmissing','stopmissing','parent','created','image','interval','threadstart','threadstop','irp_end','irp_reuse','version','none']
  for c in cases:
   with self.subTest(c=c):
    r,i=fixtures();loss=0
    if c=='loss':loss=1
    elif c=='unknownloss':loss=None
    elif c=='startmissing':r.pop(0)
    elif c=='stopmissing':r.pop()
    elif c=='parent':i['ppid']=77
    elif c=='created':i['created']=99
    elif c=='image':i['image']='elsewhere'
    elif c=='interval':i['launcher_ended']=3
    elif c=='threadstart':r.pop(1)
    elif c=='threadstop':r.pop(4)
    elif c=='irp_end':r.pop(3)
    elif c=='irp_reuse':r.append(copy.deepcopy(r[2]))
    elif c=='version':r[2]['version']=5
    elif c=='none':r=[x for x in r if x['provider']!=FILE]
    with self.assertRaises(Refusal):correlate(r,i,loss)
 def test_foreign_excluded(self):
  r,i=fixtures();r[1]['data']['ProcessID']=44;r[4]['data']['ProcessID']=44
  with self.assertRaises(Refusal):correlate(r,i,0)
if __name__=='__main__':unittest.main()
