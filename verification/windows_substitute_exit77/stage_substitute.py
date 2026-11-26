"""Diagnostic substitute-exit77 staging. Builds a derived runtime directory only.
The acquired runtime, product, original worker and canonical stager are never
modified. Only python.exe in the derived copy is replaced by the pinned
kernel32-only ExitProcess(77) image. Signature/version checks done by the
caller apply to the acquired original python.exe, not to the substitute.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

SOURCE_PINS={
 'candidate_worker.py':'d99eac2c3dafd854fd3a3e2fca7b91049cf3ef7fb19ce0656de6e5b311d0e4fa',
 'stage_runtime.py':'18ed0a0966e6b39c52e77ccec702a71a78a66cca5e109884c8df366371295af3',
 'runtime_integrity.py':'f84d1fd9021d40abf0fd264cb7ebf80f543111a54b39eb3e10ee922b8a1b4c39',
}


def sha(data):return hashlib.sha256(data).hexdigest()


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


def derive(runtime,owned,expected_owner,substitute,substitute_pin):
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
    shutil.copytree(runtime,derived)
    exe=derived/'python.exe'
    exe.unlink()
    exe.write_bytes(data)
    after=tree(derived)
    expected=dict(before);expected['python.exe']=substitute_pin
    if after!=expected:raise ValueError('Derived runtime differs beyond python.exe')
    if tree(runtime)!=before:raise ValueError('Acquired runtime changed')
    now=canonical(owned).stat()
    if (now.st_dev,now.st_ino)!=(identity.st_dev,identity.st_ino):raise ValueError('Owned root changed; retain')
    return derived,data


def main():
    p=argparse.ArgumentParser()
    for name in ('source','owned','owner','runtime','substitute','substitute-pin','product','packages-json','destination'):p.add_argument('--'+name,required=True)
    a=p.parse_args();owned=canonical(a.owned);source=canonical(a.source)
    destination=Path(a.destination).absolute()
    if destination.parent!=owned or destination.name!='staged' or destination.exists() or destination.is_symlink():
        raise ValueError('Exact fresh owned stage required')
    payload={}
    for name,pin in SOURCE_PINS.items():
        data=canonical(source/name).read_bytes()
        if sha(data)!=pin:raise ValueError('Original source mismatch')
        payload[name]=data
    derived,data=derive(a.runtime,owned,a.owner,a.substitute,a.substitute_pin)
    command=[sys.executable,'-B',str(source/'stage_runtime.py'),'--runtime',str(derived),'--product',a.product,'--packages-json',a.packages_json,'--destination',str(destination)]
    with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as error:
        result=subprocess.run(command,stdout=output,stderr=error,timeout=120,check=False)
        output.seek(0);raw=output.read(67);lines=raw.splitlines()
        if result.returncode or len(lines)!=1 or len(lines[0])!=64 or len(raw)>66 or any(x not in b'0123456789abcdef' for x in lines[0]):
            raise ValueError('Canonical staging refused; raw output withheld')
    for name,pin in SOURCE_PINS.items():
        if sha(canonical(source/name).read_bytes())!=pin:raise ValueError('Original source changed after staging')
    if canonical(destination/'runtime/python.exe').read_bytes()!=data:raise ValueError('Staged substitute identity changed')
    if canonical(destination/'runtime/candidate_worker.py').read_bytes()!=payload['candidate_worker.py']:raise ValueError('Staged original worker changed')
    print(raw.decode('ascii').strip())


if __name__=='__main__':main()
