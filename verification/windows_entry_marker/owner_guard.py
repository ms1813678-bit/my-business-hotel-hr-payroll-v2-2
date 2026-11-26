"""Fresh diagnostic-root lifecycle. Caller must establish no live process first.
No promise of safety against privileged concurrent filesystem mutation.
"""
import argparse
import json
from pathlib import Path
import shutil
from stage_marker import canonical


def create(root,owner):
    root=Path(root).absolute();canonical(root.parent)
    if not owner or root.exists() or root.is_symlink():raise ValueError('Fresh owned root required')
    root.mkdir();identity=root.stat()
    (root/'ENTRY77-OWNER.json').write_text(json.dumps({'owner':owner,'state':'preparing-entry77'}))
    return str(identity.st_dev)+':'+str(identity.st_ino)


def cleanup(root,owner,identity):
    root=canonical(root);stat=root.stat()
    if str(stat.st_dev)+':'+str(stat.st_ino)!=identity:raise ValueError('Root identity changed; retained')
    if json.loads((root/'ENTRY77-OWNER.json').read_text())!={'owner':owner,'state':'preparing-entry77'}:
        raise ValueError('Root owner changed; retained')
    for path in root.rglob('*'):canonical(path)
    # Existing trusted watchdog/host result, not filesystem/log text, is the
    # caller's prerequisite. No launch/termination is attempted here.
    shutil.rmtree(root)
    if root.exists() or root.is_symlink():raise ValueError('Owned root absence unconfirmed')


def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=('create','cleanup'))
    for name in ('root','owner'):p.add_argument('--'+name,required=True)
    p.add_argument('--identity');a=p.parse_args()
    if a.action=='create':print(create(a.root,a.owner))
    elif not a.identity:raise ValueError('Identity required')
    else:cleanup(a.root,a.owner,a.identity)


if __name__=='__main__':main()
