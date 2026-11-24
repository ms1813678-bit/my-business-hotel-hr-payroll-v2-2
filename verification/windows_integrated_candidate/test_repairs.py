import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import stage_runtime
import runtime_integrity
from candidate_parent import _run_checked


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.runtime = self.root / 'source'
        self.runtime.mkdir()
        for n in ('python.exe','python311.dll','python311.zip'):
            (self.runtime/n).write_bytes(b'SYNTHETIC')
        self.product=self.root/'product'
        (self.product/'app').mkdir(parents=True)
        for n in ('fingerprint_evidence.py','attendance_import.py'):
            (self.product/'app'/n).write_text('# synthetic')
        self.packages={}
        for n in ('openpyxl','xlrd','et_xmlfile'):
            p=self.root/n;p.mkdir();(p/'__init__.py').write_text('');self.packages[n]=p
        self.dest=self.root/'dest'

    def stage(self):
        return stage_runtime.stage(self.runtime,self.product,self.packages,self.dest)

    def test_valid_manifest(self):
        pin=self.stage();runtime_integrity.verify(self.dest,pin)
        self.assertEqual((self.dest/'runtime/python311._pth').read_text(),'python311.zip\n.\nvendor\n')

    def test_invalid_packages_allocate_nothing(self):
        self.packages.pop('xlrd')
        with self.assertRaises(ValueError):self.stage()
        self.assertFalse(self.dest.exists())

    def test_copy_failure_owned_rollback(self):
        with patch.object(stage_runtime.shutil,'copytree',side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):self.stage()
        self.assertFalse(self.dest.exists())

    def test_file_tamper(self):
        pin=self.stage();(self.dest/'runtime/python.exe').write_bytes(b'changed')
        with self.assertRaises(ValueError):runtime_integrity.verify(self.dest,pin)

    def test_extra_empty_directory(self):
        pin=self.stage();(self.dest/'extra').mkdir()
        with self.assertRaises(ValueError):runtime_integrity.verify(self.dest,pin)

    def test_manifest_pin(self):
        self.stage()
        with self.assertRaises(ValueError):runtime_integrity.verify(self.dest,'0'*64)

    def test_symlink_ancestor(self):
        link=self.root/'link';link.symlink_to(self.runtime,target_is_directory=True)
        with self.assertRaises(ValueError):runtime_integrity.canonical(link/'python.exe')

    def run_parent(self, runner):
        return _run_checked(['launcher','profile','python',str(self.root),'worker'],b'{}',{'SYSTEMROOT':'synthetic'},lambda raw,a:raw,{},runner)

    def test_parent_stdin_env_and_success(self):
        def runner(command,**kw):
            self.assertEqual(len(command),5);self.assertEqual(kw['input'],b'{}')
            self.assertEqual(kw['env'],{'SYSTEMROOT':'synthetic'});self.assertEqual(kw['timeout'],3)
            kw['stdout'].write(b'{"status":"ok"}')
            return subprocess.CompletedProcess(command,0)
        self.assertEqual(self.run_parent(runner),b'{"status":"ok"}')

    def test_parent_partial_failure_never_decoded(self):
        def runner(command,**kw):
            kw['stdout'].write(b'partial');return subprocess.CompletedProcess(command,1)
        with self.assertRaises(ValueError):self.run_parent(runner)

    def test_parent_overflow(self):
        def runner(command,**kw):
            kw['stdout'].seek(4000000);kw['stdout'].write(b'x');return subprocess.CompletedProcess(command,0)
        with self.assertRaises(ValueError):self.run_parent(runner)

    def test_parent_exact_output_boundary(self):
        def runner(command,**kw):
            kw['stdout'].write(b'x'*4000000);return subprocess.CompletedProcess(command,0)
        self.assertEqual(len(self.run_parent(runner)),4000000)

    def test_parent_timeout(self):
        def runner(command,**kw):raise subprocess.TimeoutExpired(command,3)
        with self.assertRaises(ValueError):self.run_parent(runner)

    def test_parent_oversize_request_no_spawn(self):
        def runner(*a,**kw):raise AssertionError('must not spawn')
        with self.assertRaises(ValueError):_run_checked([],b'x'*16000001,{},None,{},runner)


if __name__=='__main__':unittest.main()

class BoundaryAdditions(unittest.TestCase):
    def test_alias_validation(self):
        from candidate_parent import validate_aliases
        for aliases in ['bad',[],{}, {'SYN': True}, {'SYN':2147483648}, {'\ud800':1}]:
            with self.assertRaises(ValueError):validate_aliases(aliases)
        validate_aliases({'SYN':1})
