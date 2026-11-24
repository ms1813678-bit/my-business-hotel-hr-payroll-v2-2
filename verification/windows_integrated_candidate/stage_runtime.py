"""Candidate-only runtime staging. No download/install/ACL changes."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import uuid
from runtime_integrity import canonical, inventory


def stage(runtime, product, packages, destination):
    runtime, product = canonical(runtime), canonical(product)
    destination = Path(destination).absolute()
    canonical(destination.parent)
    if destination.exists() or destination.is_symlink():
        raise ValueError('New destination required')
    if any(destination.is_relative_to(source) for source in (runtime, product)):
        raise ValueError('Destination must not be inside source')
    if set(packages) != {'openpyxl', 'xlrd', 'et_xmlfile'}:
        raise ValueError('Exactly parser package sources required')
    sources = {name: canonical(path) for name, path in packages.items()}
    for name, source in sources.items():
        if source.name != name or not (source / '__init__.py').is_file():
            raise ValueError('Exact package directory required')
    dlls = list(runtime.glob('python3[0-9]*.dll'))
    if len(dlls) != 1:
        raise ValueError('One explicit versioned CPython DLL required')
    stem = dlls[0].stem
    for name in ('python.exe', stem + '.dll', stem + '.zip'):
        if not (runtime / name).is_file():
            raise ValueError('Explicit embeddable-style runtime required')
    if any((runtime / name).exists() for name in ('app', 'vendor', 'candidate_worker.py')):
        raise ValueError('Runtime contains candidate-reserved paths')
    for name in ('fingerprint_evidence.py', 'attendance_import.py'):
        if not (product / 'app' / name).is_file():
            raise ValueError('Required parser source absent')
    # All validation happens before destination allocation. Inventories also
    # reject symlink/reparse descendants before copying.
    for source in (runtime, product / 'app', *sources.values()):
        inventory(source)
    marker = uuid.uuid4().hex
    destination.mkdir()
    identity = destination.stat()
    try:
        (destination / 'CANDIDATE-OWNER.json').write_text(json.dumps({'owner': marker, 'state': 'incomplete'}))
        shutil.copytree(runtime, destination / 'runtime')
        app = destination / 'runtime/app'
        app.mkdir()
        (app / '__init__.py').write_text('')
        for name in ('fingerprint_evidence.py', 'attendance_import.py'):
            shutil.copyfile(product / 'app' / name, app / name)
        shutil.copyfile(Path(__file__).with_name('candidate_worker.py'), destination / 'runtime/candidate_worker.py')
        vendor = destination / 'runtime/vendor'
        vendor.mkdir()
        for name, source in sources.items():
            shutil.copytree(source, vendor / name)
        (destination / 'runtime' / (stem + '._pth')).write_text(stem + '.zip\n.\nvendor\n', encoding='ascii')
        (destination / 'CANDIDATE-OWNER.json').write_text(json.dumps({'owner': marker, 'state': 'staged-not-accepted'}))
        raw = json.dumps({'format': 1, 'files': inventory(destination)}, sort_keys=True, indent=2).encode()
        (destination / 'MANIFEST.json').write_bytes(raw)
        return hashlib.sha256(raw).hexdigest()
    except BaseException as original:
        current = destination.lstat()
        try:
            owner = json.loads((destination / 'CANDIDATE-OWNER.json').read_text())
        except (OSError, ValueError) as marker_error:
            raise RuntimeError('Staging failed; ownership marker unreadable, tree retained') from original
        if destination.is_symlink() or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino) or owner.get('owner') != marker:
            raise RuntimeError('Owned staging identity changed; retained, no recursive deletion')
        shutil.rmtree(destination)
        raise


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('--runtime', required=True)
    p.add_argument('--product', required=True)
    p.add_argument('--packages-json', required=True)
    p.add_argument('--destination', required=True)
    a = p.parse_args()
    print(stage(a.runtime, a.product, json.loads(Path(a.packages_json).read_text()), a.destination))
