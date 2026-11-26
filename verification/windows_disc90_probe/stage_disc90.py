"""disc90 staging. Builds a derived runtime directory only. The acquired runtime, product, original worker and
canonical stager are never modified. In the derived copy: python.exe is replaced by the pinned kernel32-only
probe image, and one byte copy of python314.dll is added as pydisc314.dll (sha256 verified equal). The canonical
stager then copies the derived runtime into the owned stage, and the existing ACL loop covers the copy.
No download. Refuses before launch if pinned names, hashes or the import order do not match.
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
from pe_imports import direct_imports

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
COPY_NAME='pydisc314.dll'
# Direct imports of python314.dll in import-table order: 6 ordinary names and 13 api-set names, 19 in all.
IMPORTS=['VERSION.dll','WS2_32.dll','api-ms-win-core-path-l1-1-0.dll','bcrypt.dll','ADVAPI32.dll','KERNEL32.dll','VCRUNTIME140.dll',
 'api-ms-win-crt-stdio-l1-1-0.dll','api-ms-win-crt-environment-l1-1-0.dll','api-ms-win-crt-string-l1-1-0.dll','api-ms-win-crt-runtime-l1-1-0.dll',
 'api-ms-win-crt-heap-l1-1-0.dll','api-ms-win-crt-locale-l1-1-0.dll','api-ms-win-crt-convert-l1-1-0.dll','api-ms-win-crt-math-l1-1-0.dll',
 'api-ms-win-crt-time-l1-1-0.dll','api-ms-win-crt-process-l1-1-0.dll','api-ms-win-crt-conio-l1-1-0.dll','api-ms-win-crt-filesystem-l1-1-0.dll']


def sha(data):return hashlib.sha256(data).hexdigest()


def check_pins_static():
    if len(IMPORTS) != 19 or len(set(x.lower() for x in IMPORTS)) != 19:raise ValueError('Import pin count')
    if sum(1 for x in IMPORTS if x.lower().startswith('api-ms-win-')) != 13:raise ValueError('Api-set name count')
    if sum(1 for x in IMPORTS if not x.lower().startswith('api-ms-win-')) != 6:raise ValueError('Direct name count')
    import fnmatch
    if fnmatch.fnmatch(COPY_NAME,'python3[0-9]*.dll'):raise ValueError('Copy name would match the runtime glob')


def c_import_names(source_text):
    block=re.search(r'imports\[19\]\s*=\s*\{(.*?)\};',source_text,re.S)
    if not block:raise ValueError('Probe import array not found')
    return re.findall(r'L"([^"]+)"',block.group(1))


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
    if COPY_NAME in before:raise ValueError('Copy name already present')
    if direct_imports((Path(runtime)/'python314.dll').read_bytes())!=IMPORTS:raise ValueError('python314.dll import order differs from pin')
    if c_import_names(Path(probe_source).read_text(encoding='utf-8'))!=IMPORTS:raise ValueError('Probe import list differs from pin')


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
    copy=derived/COPY_NAME
    copy.write_bytes((derived/'python314.dll').read_bytes())
    if sha(copy.read_bytes())!=RUNTIME_PINS['python314.dll']:raise ValueError('Copy hash differs from the pinned python314.dll')
    after=tree(derived)
    expected=dict(before);expected['python.exe']=substitute_pin;expected[COPY_NAME]=RUNTIME_PINS['python314.dll']
    if after!=expected:raise ValueError('Derived runtime differs beyond python.exe and the pinned copy')
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
    if sha(canonical(destination/('runtime/'+COPY_NAME)).read_bytes())!=RUNTIME_PINS['python314.dll']:raise ValueError('Staged copy identity changed')
    if canonical(destination/'runtime/candidate_worker.py').read_bytes()!=payload['candidate_worker.py']:raise ValueError('Staged original worker changed')
    print(raw.decode('ascii').strip())


if __name__=='__main__':main()
