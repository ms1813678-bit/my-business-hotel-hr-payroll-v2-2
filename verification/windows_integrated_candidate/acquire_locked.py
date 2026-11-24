"""Retrieve locked public runtime inputs into a new disposable CI root only."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request
import zipfile


def acquire(lock_path, destination):
    destination=Path(destination)
    if destination.exists():raise ValueError('Fresh acquisition root required')
    lock=json.loads(Path(lock_path).read_text())
    if set(lock)!={'python','openpyxl','xlrd','et_xmlfile'}:raise ValueError('Exact lock schema required')
    class StrictRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            from urllib.parse import urlparse
            old, new = urlparse(req.full_url), urlparse(newurl)
            if new.scheme!='https' or new.hostname!=old.hostname:
                raise ValueError('Supplier redirect host rejected')
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    opener=urllib.request.build_opener(StrictRedirect)
    destination.mkdir()
    for name,item in lock.items():
        url=item['url']
        expected_host='www.python.org' if name=='python' else 'files.pythonhosted.org'
        from urllib.parse import urlparse
        parts=urlparse(url)
        if parts.scheme!='https' or parts.hostname!=expected_host:raise ValueError('Unexpected supplier host')
        with opener.open(url,timeout=45) as response:
            data=response.read(30_000_001)
        if len(data)>30_000_000 or hashlib.sha256(data).hexdigest()!=item['sha256']:raise ValueError('Locked input hash mismatch')
        archive=destination/(name+'.zip')
        archive.write_bytes(data)
        target=destination/('runtime' if name=='python' else 'packages')
        target.mkdir(exist_ok=True)
        with zipfile.ZipFile(archive) as z:
            for member in z.infolist():
                if member.file_size>100_000_000:raise ValueError('Archive member bound')
                path=Path(member.filename)
                if path.is_absolute() or '..' in path.parts or ':' in member.filename or '\\' in member.filename:raise ValueError('Unsafe archive path')
            if sum(x.file_size for x in z.infolist())>150_000_000:raise ValueError('Archive bound')
            z.extractall(target)
    return destination


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--lock',required=True);p.add_argument('--destination',required=True)
    a=p.parse_args();acquire(a.lock,a.destination)
