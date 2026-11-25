"""Candidate-only trusted launch and strict decoder handoff, not product wiring."""
import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
from runtime_integrity import canonical, held_tree


def validate_aliases(aliases):
    if type(aliases) is not dict or not aliases or len(aliases)>100000:
        raise ValueError('Bounded explicit alias dictionary required')
    if any(not isinstance(k,str) or not k.strip() or len(k)>1000 or type(v) is not int or not 1<=v<=2147483647 for k,v in aliases.items()):
        raise ValueError('Exact bounded alias entries required')
    if sum(len(k) for k in aliases)>500000:
        raise ValueError('Aggregate aliases exceed budget')
    try:
        for k in aliases:k.encode('utf-8')
    except UnicodeError as exc:raise ValueError('UTF8 aliases required') from exc


def _run_checked(command, source_request, environment, decoder, verified_aliases, runner=subprocess.run):
    if type(source_request) is not bytes or not 1 <= len(source_request) <= 16_000_000:
        raise ValueError('Bounded request required')
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostic:
        try:
            result = runner(command, input=source_request, stdout=output, stderr=diagnostic,
                            timeout=3, check=False, env=environment, cwd=str(Path(command[3])))
        except subprocess.TimeoutExpired as exc:
            diagnostic.seek(0);detail=diagnostic.read(16384).decode('ascii',errors='replace')
            raise ValueError('Candidate complete wall budget exceeded; native='+detail) from exc
        if result.returncode != 0 or output.tell() > 4_000_000:
            diagnostic.seek(0)
            detail=diagnostic.read(16385)
            if len(detail)>16384:raise ValueError('Native diagnostic budget exceeded')
            # Only trusted native stderr: worker stderr is NUL by handle list.
            raise ValueError('Candidate refused code='+str(result.returncode)+' native='+detail.decode('ascii',errors='replace'))
        output.seek(0)
        return decoder(output.read(4_000_001), verified_aliases)


def parse_candidate(launcher, profile, staged_root, pinned_manifest_sha256, pinned_launcher_sha256,
                    source_request, verified_aliases):
    validate_aliases(verified_aliases)
    if os.name != 'nt':
        raise ValueError('Candidate requires Windows and separate native acceptance')
    launcher = canonical(launcher)
    # Parent holds the staged inventory, including runtime DLLs and paths.
    # Native launcher independently holds/compares its actual Python image.
    with held_tree(staged_root, pinned_manifest_sha256) as stage:
        runtime = stage / 'runtime'
        with launcher.open('rb') as image:
            if hashlib.sha256(image.read()).hexdigest() != pinned_launcher_sha256:
                raise ValueError('Pinned launcher mismatch')
            if hasattr(os, 'get_handle_inheritable'):
                # Python's open handle denies neither write nor delete sharing;
                # acquire an explicit native share-read-only guard as well.
                import ctypes
                kernel = ctypes.WinDLL('kernel32', use_last_error=True)
                kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
                kernel.CreateFileW.restype = ctypes.c_void_p
                kernel.CloseHandle.argtypes = [ctypes.c_void_p]
                kernel.CloseHandle.restype = ctypes.c_int
                guard = kernel.CreateFileW(str(launcher), 0x80000000, 1, None, 3, 0x00200000, None)
                if guard in (None, ctypes.c_void_p(-1).value):
                    raise ValueError('Launcher identity hold failed')
            else:
                raise ValueError('Windows handle support required')
            try:
                image.seek(0)
                if hashlib.sha256(image.read()).hexdigest() != pinned_launcher_sha256:
                    raise ValueError('Held launcher hash mismatch')
                from app.fingerprint_bounded_parse import _decode_worker
                command = [str(launcher), profile, str(runtime / 'python.exe'), str(runtime),
                           str(runtime / 'candidate_worker.py')]
                return _run_checked(command, source_request, {'SYSTEMROOT': os.environ['SYSTEMROOT']},
                                    _decode_worker, verified_aliases)
            finally:
                if not kernel.CloseHandle(guard):
                    raise ValueError('Launcher hold cleanup unconfirmed')
