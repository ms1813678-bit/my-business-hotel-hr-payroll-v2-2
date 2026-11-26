"""disc91 staging. Builds a derived runtime directory only. The acquired runtime, product, original worker and
canonical stager are never modified. In the derived copy: python.exe is replaced by the pinned kernel32-only
probe image, and five byte-patched copies of the pinned python314.dll are added as pydisc91_v0..v4.dll, each
verified against a sha256 pin computed on Linux (see pe_patch.py). The canonical stager then copies the derived
runtime into the owned stage, and the existing ACL loop covers the copies. No download.
Refuses before launch if pinned names, hashes or PE facts do not match.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0,str(Path(__file__).resolve().parent))
import pe_patch

SOURCE_PINS={
 'candidate_worker.py':'d99eac2c3dafd854fd3a3e2fca7b91049cf3ef7fb19ce0656de6e5b311d0e4fa',
 'stage_runtime.py':'18ed0a0966e6b39c52e77ccec702a71a78a66cca5e109884c8df366371295af3',
 'runtime_integrity.py':'f84d1fd9021d40abf0fd264cb7ebf80f543111a54b39eb3e10ee922b8a1b4c39',
}
# From the pinned CPython 3.14.10 embed zip (sha256 90905e64...4159), see ACQUISITION-LOCK.json.
RUNTIME_PINS={
 'python314.dll':'d8a06cc6ca3348925ea29e343e1c5c5895fe0bb433ff913e1cee44caf358aa6a',
 'vcruntime140.dll':'d1f4225df2cd877dbf130d5668a021dce3f94118455ff5ec952061c30afc9ce7',
 'libffi-8.dll':'eff52743773eb550fcc6ce3efc37c85724502233b6b002a35496d828bd7b280a',
}
VARIANT_PINS={
 'v0':'bd9588b9406e14f0e5ae8168f78bcfdefaebc4964fa3ee29027080a5d4264299',
 'v1':'df6e78b9f897e3e256b9ab56e8fc7395bab62c64e1dc30019e0d2bf60318cfbd',
 'v2':'649604e64511343f9351881c37658cccd2b3e3dc6575c4d57089f8cf83ce0715',
 'v3':'b0954e81860a8bcea019c6679427de02fb5cdf047a7a8b253c4d9ea6da65a15a',
 'v4':'d0ee45239759a494561a6b67d8a8a1551efc8ad1f9a80b60376683d02c04f9d5',
}


def sha(data):return hashlib.sha256(data).hexdigest()


def check_pins_static():
    import fnmatch
    if set(VARIANT_PINS)!=set(pe_patch.VARIANTS) or len(set(VARIANT_PINS.values()))!=5:raise ValueError('Variant pin set')
    if RUNTIME_PINS['python314.dll']!=pe_patch.ORIGINAL_SHA256:raise ValueError('Original pin mismatch')
    for name in pe_patch.NAMES.values():
        if fnmatch.fnmatch(name,'python3[0-9]*.dll'):raise ValueError('Variant name would match the runtime glob')


def canonical(path):
    path=Path(path).absolute()
    for entry in (path,*path.parents):
        st=entry.lstat()
        if entry.is_symlink() or getattr(st,'st_file_attributes',0)&0x400:
            raise ValueError('Reparse/symlink path refused')
    if path.resolve()!=path:raise ValueError('Canonical path required')
    return path


def tree(root):
    out={}
    for path in sorted(Path(root).rglob('*')):
        canonical(path)
        if path.is_file():out[path.relative_to(root).as_posix()]=sha(path.read_bytes())
        elif not path.is_dir():raise ValueError('Unexpected runtime entry type')
    return out


def verify_runtime(before,runtime,probe_source):
    check_pins_static()
    for name,pin in RUNTIME_PINS.items():
        if before.get(name)!=pin:raise ValueError('Pinned runtime member absent or changed: '+name)
    for name in pe_patch.NAMES.values():
        if name in before:raise ValueError('Variant name already present')
    original=(Path(runtime)/'python314.dll').read_bytes()
    pe_patch.verify_facts(original)
    for key,name in pe_patch.NAMES.items():
        if sha(pe_patch.make(original,key))!=VARIANT_PINS[key]:raise ValueError('Variant pin mismatch '+key)
    text=Path(probe_source).read_text(encoding='utf-8')
    for name in pe_patch.NAMES.values():
        if 'L"'+name+'"' not in text:raise ValueError('Probe does not name '+name)


def derive(runtime,owned,expected_owner,substitute,substitute_pin,probe_source):
    runtime=canonical(runtime);owned=canonical(owned)
    record=json.loads((owned/'ENTRY77-OWNER.json').read_text())
    if type(record) is not dict or set(record)!={'owner','state'} or record['state']!='preparing-entry77' or not expected_owner or record['owner']!=expected_owner:
        raise ValueError('Exact owner record required')
    identity=owned.stat()
    data=canonical(substitute).read_bytes()
    if sha(data)!=substitute_pin or len(data)>16384 or data[:2]!=b'MZ':
        raise ValueError('Pinned substitute image required')
    derived=owned/'derived-runtime'
    if derived.exists() or derived.is_symlink():raise ValueError('New derived runtime required')
    before=tree(runtime)
    if 'python.exe' not in before:raise ValueError('Runtime python.exe required')
    verify_runtime(before,runtime,probe_source)
    shutil.copytree(runtime,derived)
    exe=derived/'python.exe'
    exe.unlink()
    exe.write_bytes(data)
    original=(derived/'python314.dll').read_bytes()
    expected=dict(before);expected['python.exe']=substitute_pin
    for key,name in pe_patch.NAMES.items():
        blob=pe_patch.make(original,key)
        if sha(blob)!=VARIANT_PINS[key]:raise ValueError('Variant hash differs from pin '+key)
        (derived/name).write_bytes(blob);expected[name]=VARIANT_PINS[key]
    after=tree(derived)
    if after!=expected:raise ValueError('Derived runtime differs beyond python.exe and the five pinned variants')
    if tree(runtime)!=before:raise ValueError('Acquired runtime changed')
    now=canonical(owned).stat()
    if (now.st_dev,now.st_ino)!=(identity.st_dev,identity.st_ino):raise ValueError('Owned root changed; retain')
    return derived,data


def main():
    p=argparse.ArgumentParser()
    for name in ('source','owned','owner','runtime','substitute','substitute-pin','product','packages-json','destination','probe-source'):p.add_argument('--'+name,required=True)
    a=p.parse_args();owned=canonical(a.owned);source=canonical(a.source)
    destination=Path(a.destination).absolute()
    if destination.parent!=owned or destination.name!='staged' or destination.exists() or destination.is_symlink():
        raise ValueError('Exact fresh owned stage required')
    payload={}
    for name,pin in SOURCE_PINS.items():
        data=canonical(source/name).read_bytes()
        if sha(data)!=pin:raise ValueError('Original source mismatch')
        payload[name]=data
    derived,data=derive(a.runtime,owned,a.owner,a.substitute,a.substitute_pin,a.probe_source)
    command=[sys.executable,'-B',str(source/'stage_runtime.py'),'--runtime',str(derived),'--product',a.product,'--packages-json',a.packages_json,'--destination',str(destination)]
    with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as error:
        result=subprocess.run(command,stdout=output,stderr=error,timeout=120,check=False)
        output.seek(0);raw=output.read(67);lines=raw.splitlines()
        if result.returncode or len(lines)!=1 or len(lines[0])!=64 or len(raw)>66 or any(x not in b'0123456789abcdef' for x in lines[0]):
            raise ValueError('Canonical staging refused; raw output withheld')
    for name,pin in SOURCE_PINS.items():
        if sha(canonical(source/name).read_bytes())!=pin:raise ValueError('Original source changed after staging')
    if canonical(destination/'runtime/python.exe').read_bytes()!=data:raise ValueError('Staged substitute identity changed')
    for key,name in pe_patch.NAMES.items():
        if sha(canonical(destination/('runtime/'+name)).read_bytes())!=VARIANT_PINS[key]:raise ValueError('Staged variant identity changed '+key)
    if canonical(destination/'runtime/candidate_worker.py').read_bytes()!=payload['candidate_worker.py']:raise ValueError('Staged original worker changed')
    print(raw.decode('ascii').strip())


if __name__=='__main__':main()
