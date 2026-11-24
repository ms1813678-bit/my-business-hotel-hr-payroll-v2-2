"""Candidate-only canonical inventory and held-runtime integrity checks."""
from contextlib import ExitStack, contextmanager
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath


def canonical(path):
    path = Path(os.path.abspath(path))
    for entry in (path, *path.parents):
        st = entry.lstat()
        if entry.is_symlink() or getattr(st, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Reparse/symlink path rejected')
    if path.resolve() != path:
        raise ValueError('Canonical path required')
    return path


def inventory(root):
    root = canonical(root)
    result = {}
    for path in sorted(root.rglob('*')):
        canonical(path)
        key = path.relative_to(root).as_posix()
        if key == 'MANIFEST.json':
            continue
        if path.is_dir():
            result[key] = {'kind': 'directory'}
        elif path.is_file():
            result[key] = {'kind': 'file', 'size': path.stat().st_size,
                           'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
        else:
            raise ValueError('Regular files and directories only')
    return result


def verify(root, pinned_manifest_sha256):
    root = canonical(root)
    raw = (root / 'MANIFEST.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != pinned_manifest_sha256:
        raise ValueError('Pinned manifest mismatch')
    data = json.loads(raw)
    if type(data) is not dict or set(data) != {'format', 'files'} or data['format'] != 1:
        raise ValueError('Invalid manifest schema')
    if data['files'] != inventory(root):
        raise ValueError('Runtime tree differs from manifest')
    for name in data['files']:
        p = PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name:
            raise ValueError('Unsafe manifest path')
    return data


@contextmanager
def held_tree(root, pinned_manifest_sha256):
    """Windows denies write/delete sharing while all runtime identities are held.
    Re-hash after holds, then verify canonical paths a second time. This does not
    replace RX ACL provisioning or assert protection from privileged writers.
    """
    if os.name != 'nt':
        raise ValueError('Native held-tree check requires Windows')
    root = canonical(root)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handles = []
    clean = True
    try:
        paths = [root, *sorted(root.rglob('*'))]
        for path in paths:
            canonical(path)
            flags = 0x02000000 | 0x00200000  # BACKUP_SEMANTICS, OPEN_REPARSE_POINT
            handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, flags, None)
            if handle in (None, ctypes.c_void_p(-1).value):
                raise ValueError('Runtime identity could not be held')
            handles.append(handle)
        verify(root, pinned_manifest_sha256)
        for path in paths:
            canonical(path)
        yield root
    finally:
        for handle in reversed(handles):
            if not kernel.CloseHandle(handle):
                clean = False
        if not clean:
            raise ValueError('Runtime hold cleanup unconfirmed')
