"""Read-only dependency smoke for a disposable Windows acceptance environment.

Does not register or start services, open windows or listeners, write firewall or
registry settings, read app configuration/secrets, or initialize a database.
Importing native dependencies is execution. Never use on the hotel PC as a
substitute for an approved installation. Passing is not native app acceptance.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import struct
import sys

MODULES = (
    'fastapi', 'uvicorn', 'sqlalchemy', 'psycopg', 'jinja2', 'multipart',
    'pwdlib', 'itsdangerous', 'pydantic_settings', 'alembic', 'PIL',
    'openpyxl', 'reportlab', 'arabic_reshaper', 'bidi', 'cryptography', 'xlrd',
    'win32service', 'win32serviceutil', 'servicemanager', 'pythoncom',
    'pywintypes', 'webview', 'clr',
)
DISTRIBUTIONS = (
    'pywebview', 'pywin32', 'pythonnet', 'clr-loader', 'psycopg',
    'psycopg-binary', 'cryptography', 'proxy-tools',
)


def run_imports(importer=importlib.import_module):
    results = []
    for name in MODULES:
        try:
            importer(name)
        except Exception as exc:
            # Package exception text can include local paths or environment.
            results.append({'module': name, 'imported': False,
                            'exception_type': type(exc).__name__})
        else:
            results.append({'module': name, 'imported': True})
    return results


def main():
    if (sys.platform != 'win32' or sys.version_info[:2] != (3, 11)
            or struct.calcsize('P') != 8):
        print(json.dumps({'status': 'refused',
                          'reason': 'Requires Windows CPython3.11 64-bit.'}))
        return 2
    if platform.python_implementation() != 'CPython':
        print(json.dumps({'status': 'refused', 'reason': 'Requires CPython.'}))
        return 2
    versions = {}
    for name in DISTRIBUTIONS:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    if versions['pywebview'] != '5.4':
        print(json.dumps({'status': 'refused', 'reason': 'Exact pywebview5.4 required.'}))
        return 2
    results = run_imports()
    # Static pin contract evidence. This does not instantiate a WebView2 control
    # or establish that the installed WebView2 runtime is compatible.
    webview_hashes = {}
    dist = importlib.metadata.distribution('pywebview')
    for rel in ('webview/platforms/edgechromium.py', 'webview/platforms/winforms.py'):
        p = Path(dist.locate_file(rel))
        if p.is_file():
            webview_hashes[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    passed = all(r['imported'] for r in results)
    print(json.dumps({
        'status': 'imports-passed' if passed else 'imports-failed',
        'native_app_acceptance': False,
        'windows_version': platform.version(),
        'python_version': platform.python_version(),
        'python_implementation': platform.python_implementation(),
        'pointer_bits': struct.calcsize('P') * 8,
        'versions': versions,
        'modules': results,
        'installed_pywebview_source_sha256': webview_hashes,
        'unverified': ['WebView2 control initialization', 'SCM dispatch/logon',
                       'independent backend lifecycle', 'effective ACLs',
                       'TLS/LAN/phone', 'PDF/download/print/unsaved editor',
                       'installer/uninstaller/rollback'],
    }, sort_keys=True))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
