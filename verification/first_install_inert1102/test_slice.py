"""Pure source tests only. No Windows calls, filesystem mutation or service."""
import unittest
from slice import Slice, Result, Kind, Refused, PAYLOAD, fixture_id

class Fake:
    def __init__(self, fail=None, unknown=False):
        self.tokens = {}; self.removed = []; self.fail = fail; self.unknown = unknown
        self.pinned = False; self.journals = []; self.ops = []
    def preflight(self): self.ops.append("preflight")
    def make(self, name):
        self.ops.append(name)
        if self.fail == name and self.unknown: return Result(Kind.UNKNOWN, detail="injected")
        token = object(); self.tokens[token] = name
        return Result(Kind.OWNED, token, "injected" if self.fail == name else "")
    def create_root(self): return self.make("root")
    def create_file(self, root, name, data): return self.make(name)
    def create_service(self, root): return self.make("service")
    def issued(self, token, operation): return self.tokens.get(token) == operation
    def verify_file(self, token, expected):
        if self.fail == "verify": raise Refused("verify")
    def verify_service(self, token):
        if self.fail == "service-verify": raise Refused("service verify")
    def journal(self, root, events):
        self.journals.append(tuple(events))
        if self.fail == "journal": raise Refused("journal")
    def pin_root(self): self.pinned = True
    def remove(self, token):
        name = self.tokens[token]
        if name in self.removed: return
        if self.fail == "remove-" + name: raise Refused("cleanup")
        if name == "root" and self.pinned: raise Refused("pinned")
        self.removed.append(name)

class Tests(unittest.TestCase):
    def test_success_staged_not_ready(self):
        a=Fake();s=Slice(a)
        self.assertEqual(s.run(),"staged-not-ready");self.assertEqual(s.rollback(),[])
        self.assertEqual(a.removed,["service","manifest.json","payload.txt","root"])
        self.assertEqual(s.rollback(),[])
        with self.assertRaises(Refused):s.run()
    def test_failed_owned_keeps_cleanup_authority(self):
        for name in ("root","payload.txt","manifest.json","service"):
            with self.subTest(name=name):
                a=Fake(name);s=Slice(a)
                with self.assertRaises(Refused):s.run()
                self.assertEqual(s.rollback(),[])
                self.assertIn(name,a.removed)
    def test_unknown_preserves_release(self):
        for name in ("root","payload.txt","service"):
            with self.subTest(name=name):
                a=Fake(name,True);s=Slice(a)
                with self.assertRaises(Refused):s.run()
                self.assertTrue(s.rollback());self.assertEqual(a.removed,[])
    def test_service_removal_failure_pins_all_files(self):
        a=Fake();s=Slice(a);s.run();a.fail="remove-service"
        self.assertTrue(s.rollback());self.assertEqual(a.removed,[])
    def test_independent_file_cleanup_and_pinned_root(self):
        a=Fake();s=Slice(a);s.run();a.fail="remove-manifest.json"
        self.assertTrue(s.rollback());self.assertIn("payload.txt",a.removed)
        self.assertNotIn("root",a.removed)
    def test_verification_and_journal_failure_cleanup(self):
        for name in ("verify","service-verify","journal"):
            with self.subTest(name=name):
                a=Fake(name);s=Slice(a)
                with self.assertRaises(Refused):s.run()
                self.assertEqual(s.rollback(),[])
    def test_forged_result(self):
        a=Fake();a.create_root=lambda:Result(Kind.OWNED,object());s=Slice(a)
        with self.assertRaises(Refused):s.run()
        self.assertTrue(s.unknown);self.assertTrue(s.rollback())
    def test_malformed_and_exception_are_unknown(self):
        for create in (lambda: "bad",lambda: (_ for _ in ()).throw(ValueError("bad"))):
            a=Fake();a.create_root=create;s=Slice(a)
            with self.assertRaises(Exception):s.run()
            self.assertTrue(s.unknown);self.assertTrue(s.rollback())
    def test_no_effect_collision_has_no_owned_token(self):
        a=Fake();a.create_root=lambda:Result(Kind.NO_EFFECT,detail="collision");s=Slice(a)
        with self.assertRaises(Refused):s.run()
        self.assertEqual(s.owned,[]);self.assertEqual(s.rollback(),[])
    def test_grammar(self):
        self.assertEqual(fixture_id("0"*32),"0"*32)
        for value in ("..","x"*32,"0"*31,"0"*33):
            with self.assertRaises(Refused):fixture_id(value)
    def test_no_start_or_product_target(self):
        from pathlib import Path
        source=Path("native.py").read_text()
        self.assertNotIn("StartService(",source);self.assertNotIn("ControlService(",source)
        self.assertNotIn("HOnYBackend",source);self.assertNotIn("rmtree",source)
        self.assertIn("SERVICE_DISABLED",source)

if __name__=="__main__": unittest.main()
