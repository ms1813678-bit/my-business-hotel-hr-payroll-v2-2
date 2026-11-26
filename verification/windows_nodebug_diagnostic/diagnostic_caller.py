"""DIAGNOSTIC ONLY. One fixed synthetic payload, trusted watchdog exit mapping.
Separate diagnostic lifecycle, never a product entrypoint. No retry, no acceptance path.
Caller MUST convert CleanupUnconfirmed to exit 125 and retain profile + stage.
"""
import contextlib
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile


class CleanupUnconfirmed(RuntimeError):
    """No teardown of the profile or stage is safe on this result."""


EXIT_STATUS = {0: 'returned', 122: 'refused', 123: 'output_refused',
               124: 'timeout', 125: 'cleanup_unconfirmed', 126: 'unavailable'}
FIXED_RAW = b'ID,Date,Time\nOTHER,2026-11-01,09:00\nSYN,2026-11-01,08:00 08:00 16:00\n'


@contextlib.contextmanager
def held_binary(path, pin):
    """Guarded native share-read-only identity. Does not write the executable."""
    if os.name != 'nt':
        raise ValueError('Windows required')
    if len(pin) != 64 or any(c not in '0123456789abcdef' for c in pin):
        raise ValueError('Exact binary pin required')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32,
                                  ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p]
    kernel.CreateFileW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    path = Path(path).resolve(strict=True)
    class Information(ctypes.Structure):
        _fields_ = [('attributes', ctypes.c_uint32), ('times', ctypes.c_uint32 * 6),
                    ('volume', ctypes.c_uint32), ('size_high', ctypes.c_uint32),
                    ('size_low', ctypes.c_uint32), ('links', ctypes.c_uint32),
                    ('index_high', ctypes.c_uint32), ('index_low', ctypes.c_uint32)]
    kernel.GetFileInformationByHandle.argtypes = [ctypes.c_void_p, ctypes.POINTER(Information)]
    kernel.GetFileInformationByHandle.restype = ctypes.c_int
    kernel.GetFinalPathNameByHandleW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
    kernel.GetFinalPathNameByHandleW.restype = ctypes.c_uint32
    kernel.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                               ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
    kernel.ReadFile.restype = ctypes.c_int
    handle = kernel.CreateFileW(str(path), 0x80000000, 1, None, 3, 0x00200000, None)
    if handle in (None, ctypes.c_void_p(-1).value):
        raise ValueError('Diagnostic binary hold refused')
    try:
        information = Information()
        if not kernel.GetFileInformationByHandle(handle, ctypes.byref(information)) or information.attributes & (0x400 | 0x10):
            raise ValueError('Diagnostic executable must be a non-reparse regular file')
        final = ctypes.create_unicode_buffer(32768)
        length = kernel.GetFinalPathNameByHandleW(handle, final, len(final), 0)
        if not length or length >= len(final):
            raise ValueError('Diagnostic executable identity unavailable')
        if os.path.normcase(os.path.normpath(final.value.removeprefix('\\\\?\\'))) != os.path.normcase(str(path)):
            raise ValueError('Held executable final identity differs')
        # Hash the held object, never reopen by name after taking the hold.
        checksum = hashlib.sha256()
        buffer = ctypes.create_string_buffer(65536)
        count = ctypes.c_uint32()
        while True:
            if not kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(count), None):
                raise ValueError('Held executable read refused')
            if count.value == 0:
                break
            checksum.update(buffer.raw[:count.value])
        if checksum.hexdigest() != pin:
            raise ValueError('Held diagnostic binary hash differs')
        yield path
    finally:
        if not kernel.CloseHandle(handle):
            raise CleanupUnconfirmed('Diagnostic executable hold cleanup unconfirmed')


def observe_once(*, watchdog, watchdog_pin, launcher, launcher_pin, profile,
                 staged_root, manifest_pin, decoder, progress=None):
    """Must run only inside the existing owned profile/verified-RX-stage lifecycle.

    Both holds and held_tree must outlive the native process and its cleanup.
    Any unknown watchdog exit, launch ambiguity or timeout is safety failure.
    Ordinary known diagnostic refusal does not replace the original harness result.
    """
    def mark(stage, native_exit=None):
        if progress is not None:progress(stage, native_exit)
    mark('request')
    import base64
    from runtime_integrity import held_tree
    request = json.dumps({'payload': base64.b64encode(FIXED_RAW).decode('ascii'),
                          'filename': 'synthetic.csv', 'aliases': {'SYN': 1}},
                         separators=(',', ':')).encode('ascii')
    if os.name != 'nt':
        return {'status': 'unavailable', 'cleanup_confirmed': True, 'decoded': False}
    if any(key not in os.environ for key in ('LOCALAPPDATA', 'SYSTEMROOT')):
        return {'status': 'unavailable', 'cleanup_confirmed': True, 'decoded': False}
    # These are fresh exclusive, non-append temporary regular files. No pipes.
    mark('integrity_holds')
    with held_tree(staged_root, manifest_pin) as stage, held_binary(watchdog, watchdog_pin) as outer, held_binary(launcher, launcher_pin) as native:
        runtime = stage / 'runtime'
        # The verified staged vendor tree, not ambient site-packages, supplies
        # the direct parser's unconditional openpyxl import even for CSV.
        mark('package_preflight')
        import importlib
        import sys
        vendor = runtime / 'vendor'
        before_path = list(sys.path)
        try:
            sys.path.insert(0, str(vendor))
            for name in ('et_xmlfile', 'openpyxl', 'xlrd'):
                module = importlib.import_module(name)
                origin = Path(module.__file__).resolve(strict=True)
                if not origin.is_relative_to((vendor / name).resolve(strict=True)):
                    raise ValueError('Direct parser vendor origin refused')
            mark('direct_oracle')
            from app.fingerprint_evidence import parse_fingerprint_evidence
            direct = parse_fingerprint_evidence(FIXED_RAW, filename='synthetic.csv', verified_aliases={'SYN': 1})
            expected = (direct.period_start, direct.period_end,
                        [dict(v) for v in direct.occurrences], [dict(v) for v in direct.issues])
        finally:
            sys.path[:] = before_path
        with tempfile.TemporaryFile() as source, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as diagnostic:
            source.write(request)
            source.flush()
            source.seek(0)
            command = [str(outer), str(native), profile, str(runtime / 'python.exe'),
                       str(runtime), str(runtime / 'candidate_worker.py')]
            environment = {key: os.environ[key] for key in ('LOCALAPPDATA', 'SYSTEMROOT')}
            mark('watchdog_launch')
            try:
                child = subprocess.Popen(command, stdin=source, stdout=output, stderr=diagnostic,
                                         env=environment, cwd=str(runtime), close_fds=True)
            except OSError as exc:
                # Popen did not return a handle. Be conservative, do not infer no child.
                raise CleanupUnconfirmed('Watchdog launch outcome unconfirmed') from exc
            mark('watchdog_started')
            mark('watchdog_wait')
            try:
                result = child.wait(timeout=28)
            except BaseException as exc:
                # Terminating the watchdog closes its job, but that is not accounting proof.
                # Retain profile/stage even if the watchdog is eventually reaped.
                try:
                    child.kill()
                    child.wait(timeout=5)
                    child.__exit__(None, None, None)
                except BaseException:
                    pass
                raise CleanupUnconfirmed('Watchdog termination outcome unconfirmed') from exc
            # wait completed, release Python's process handle deterministically.
            child.__exit__(None, None, None)
            mark('watchdog_exit',result)
            status = EXIT_STATUS.get(result)
            if status is None or result == 125:
                raise CleanupUnconfirmed('Watchdog tree cleanup unconfirmed')
            # The native return mapping is authoritative, stderr text is never authority.
            # Disk thresholds are SOFT, returned data hard-bounded independently here.
            size = os.fstat(output.fileno()).st_size
            diagnostic_size = os.fstat(diagnostic.fileno()).st_size
            if size > 4_000_000 or diagnostic_size > 66_560:
                return {'status': 'output_refused', 'cleanup_confirmed': True, 'decoded': False}
            if result != 0:
                return {'status': status, 'cleanup_confirmed': True, 'decoded': False}
            mark('decode')
            output.seek(0)
            raw = output.read(4_000_001)
            try:
                decoded = decoder(raw, {'SYN': 1})
                valid = (type(decoded) is tuple and len(decoded) == 4 and
                         decoded == expected)
            except Exception:
                valid = False
            return {'status': 'returned' if valid else 'decode_refused',
                    'cleanup_confirmed': True, 'decoded': valid}
