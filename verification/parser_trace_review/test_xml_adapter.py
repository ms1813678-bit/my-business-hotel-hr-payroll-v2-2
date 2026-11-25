import unittest,tempfile,pathlib
from xml_adapter import load,stamp,number
from correlate import Refusal,FILE
class AdapterTests(unittest.TestCase):
 def parse(self,text):
  with tempfile.TemporaryDirectory() as t:
   p=pathlib.Path(t)/'own.xml';p.write_text(text);return load(p)
 def fixture(self,version=1):
  return '<Events><Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System><Provider Guid="{'+FILE+'}"/><EventID>12</EventID><Version>'+str(version)+'</Version><TimeCreated SystemTime="2026-11-25T05:20:00.123456700Z"/></System><EventData><Data Name="Irp">0xAB</Data><Data Name="IssuingThreadId">20</Data><Data Name="FileName">C:/owned/a.dll</Data></EventData></Event></Events>'
 def test_valid(self):
  r=self.parse(self.fixture());self.assertEqual(r[0]['data']['Irp'],171)
  self.assertEqual(r[0]['ts']%1000000000,123456700)
 def test_refusal(self):
  for s in (self.fixture(3),self.fixture().replace('IssuingThreadId','unknown'),self.fixture().replace('</EventData>','<Data Name="Irp">1</Data></EventData>'),self.fixture().replace('123456700Z','123456700+00:00'),'<!DOCTYPE Events [<!ENTITY a "x">]>'+self.fixture()):
   with self.subTest(s=s[:100]):
    with self.assertRaises(Refusal):self.parse(s)
 def test_numeric(self):
  for s in ('-1',' 5','0x','18446744073709551616','1.2'):
   with self.assertRaises(Refusal):number(s)
if __name__=='__main__':unittest.main()

class ReviewRegressionTests(unittest.TestCase):
 parse=AdapterTests.parse
 fixture=AdapterTests.fixture
 def test_review_findings_refuse(self):
  for s in (self.fixture().replace('Guid=', 'NoGuid='),self.fixture().replace('<EventID>12</EventID>','<EventID>12</EventID><EventID>99</EventID>'),'<Events><X/></Events>',self.fixture().replace('<EventData>','<EventData>'+('<X>'*20)).replace('</EventData>',('</X>'*20)+'</EventData>')):
   with self.assertRaises(Refusal):self.parse(s)
 def test_large_junk_refuses_early(self):
  import tracemalloc
  with tempfile.TemporaryDirectory() as t:
   p=pathlib.Path(t)/'junk.xml';p.write_text('<Events>'+'<X/>'*300000+'</Events>')
   tracemalloc.start()
   try:
    with self.assertRaises(Refusal):load(p)
    _,peak=tracemalloc.get_traced_memory();self.assertLess(peak,2000000)
   finally:tracemalloc.stop()

 def test_large_event_junk_refuses_early(self):
  import tracemalloc
  with tempfile.TemporaryDirectory() as t:
   p=pathlib.Path(t)/'junk.xml';p.write_text(self.fixture().replace('</EventData>','<X/>'*300000+'</EventData>'))
   tracemalloc.start()
   try:
    with self.assertRaises(Refusal):load(p)
    _,peak=tracemalloc.get_traced_memory();self.assertLess(peak,2000000)
   finally:tracemalloc.stop()
