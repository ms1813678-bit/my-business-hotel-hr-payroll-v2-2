"""Diagnostic entry77 only. Copy source before byte-identical canonical stager.
Never modifies the original worker, product, runtime or existing stage.
Any uncertain owned-root identity is retained for the workflow owner to handle.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ORIGINAL_WORKER='d99eac2c3dafd854fd3a3e2fca7b91049cf3ef7fb19ce0656de6e5b311d0e4fa'
SOURCE_PINS={
 'candidate_worker.py':ORIGINAL_WORKER,
 'stage_runtime.py':'18ed0a0966e6b39c52e77ccec702a71a78a66cca5e109884c8df366371295af3',
 'runtime_integrity.py':'f84d1fd9021d40abf0fd264cb7ebf80f543111a54b39eb3e10ee922b8a1b4c39',
}
MARKER=b'raise SystemExit(77)\n'


def canonical(path):
    path=Path(path).absolute()
    for entry in (path,*path.parents):
        st=entry.lstat()
        if entry.is_symlink() or getattr(st,'st_file_attributes',0)&0x400:
            raise ValueError('Reparse/symlink path refused')
    if path.resolve()!=path:raise ValueError('Canonical path required')
    return path


def prepare(source,owned,expected_owner):
    source=canonical(source);owned=canonical(owned)
    # Explicit fresh-root sentinel, bound to this single workflow run.
    record=json.loads((owned/'ENTRY77-OWNER.json').read_text())
    if type(record) is not dict or set(record)!={'owner','state'} or record['state']!='preparing-entry77' or not isinstance(record['owner'],str) or record['owner']!=expected_owner or not expected_owner:
        raise ValueError('Exact marker owner record required')
    identity=owned.stat()
    if source==owned or source.is_relative_to(owned) or owned.is_relative_to(source):
        raise ValueError('Separate source and owned root required')
    payload={}
    for name,pin in SOURCE_PINS.items():
        file=canonical(source/name)
        data=file.read_bytes()
        if hashlib.sha256(data).hexdigest()!=pin:raise ValueError('Original diagnostic source mismatch')
        payload[name]=data
    marker=(Path(__file__).with_name('worker_entry77.py')).read_bytes()
    if marker!=MARKER:raise ValueError('Exact import-free marker required')
    dest=owned/'entry77-source'
    if dest.exists() or dest.is_symlink():raise ValueError('New source-copy destination required')
    dest.mkdir()
    # No recursive cleanup here. A partial failure can be removed prelaunch
    # by the outer owner after identity checks. No retention is promised.
    for name,data in payload.items():(dest/name).write_bytes(MARKER if name=='candidate_worker.py' else data)
    if set(x.name for x in dest.iterdir())!=set(SOURCE_PINS):raise ValueError('Exact source-copy inventory required')
    for name,data in payload.items():
        expected=MARKER if name=='candidate_worker.py' else data
        if canonical(dest/name).read_bytes()!=expected:raise ValueError('Source-copy identity changed')
        if canonical(source/name).read_bytes()!=data:raise ValueError('Original source changed')
    now=canonical(owned).stat()
    if (now.st_dev,now.st_ino)!=(identity.st_dev,identity.st_ino) or json.loads((owned/'ENTRY77-OWNER.json').read_text())!=record:
        raise ValueError('Owned root changed; retain')
    return dest


def main():
    p=argparse.ArgumentParser()
    for name in ('source','owned','owner','runtime','product','packages-json','destination'):p.add_argument('--'+name,required=True)
    a=p.parse_args();owned=canonical(a.owned)
    destination=Path(a.destination).absolute()
    if destination.parent!=owned or destination.name!='staged' or destination.exists() or destination.is_symlink():
        raise ValueError('Exact fresh owned stage required')
    copy=prepare(a.source,owned,a.owner)
    command=[sys.executable,'-B',str(copy/'stage_runtime.py'),'--runtime',a.runtime,'--product',a.product,'--packages-json',a.packages_json,'--destination',str(destination)]
    with tempfile.TemporaryFile() as output,tempfile.TemporaryFile() as error:
        result=subprocess.run(command,stdout=output,stderr=error,timeout=120,check=False)
        output.seek(0);raw=output.read(67)
        lines=raw.splitlines()
        if result.returncode or len(lines)!=1 or len(lines[0])!=64 or len(raw)>66 or any(x not in b'0123456789abcdef' for x in lines[0]):
            raise ValueError('Canonical marker staging refused; raw output withheld')
        for name,pin in SOURCE_PINS.items():
            if hashlib.sha256(canonical(Path(a.source)/name).read_bytes()).hexdigest()!=pin:raise ValueError('Original source changed after staging')
            expected=MARKER if name=='candidate_worker.py' else (Path(a.source)/name).read_bytes()
            if canonical(copy/name).read_bytes()!=expected:raise ValueError('Source copy changed after staging')
        if set(x.name for x in copy.iterdir())!=set(SOURCE_PINS):raise ValueError('Source copy gained entries')
        print(raw.decode('ascii').strip())


if __name__=='__main__':main()
