"""Isolated original-source parser process. No accounts or database connection."""
import base64,json,sys,os
from datetime import date,datetime


def main():
    if os.name=='posix':
        import resource
        resource.setrlimit(resource.RLIMIT_CPU,(2,2))
        resource.setrlimit(resource.RLIMIT_FSIZE,(4_000_000,4_000_000))
        resource.setrlimit(resource.RLIMIT_AS,(512*1024*1024,512*1024*1024))
    elif os.name=='nt':
        # The parent places this process in a job object (CPU, memory, process count) before it runs.
        # Refuse to parse anything if those limits are not in force.
        from app.fingerprint_windows_job import verify_current_process_is_bounded
        if not verify_current_process_is_bounded():
            raise RuntimeError('Operational parser isolation is not in force.')
    else:
        raise RuntimeError('Operational parser isolation requires platform acceptance before activation.')
    from app.fingerprint_evidence import parse_fingerprint_evidence
    values=json.loads(sys.stdin.buffer.read(16_000_001))
    payload=base64.b64decode(values['payload'],validate=True)
    try:
        result=parse_fingerprint_evidence(payload,filename=values['filename'],verified_aliases=values['aliases'])
        output=dict(status='ok',period_start=result.period_start,period_end=result.period_end,
            occurrences=[dict(v) for v in result.occurrences],issues=[dict(v) for v in result.issues])
    except ValueError:output=dict(status='refused')
    def serial(value):
        if type(value) in (date,datetime):return value.isoformat()
        raise TypeError('Unsupported parser output.')
    sys.stdout.buffer.write(json.dumps(output,default=serial,ensure_ascii=False,allow_nan=False,separators=(',',':')).encode())


if __name__=='__main__':main()
