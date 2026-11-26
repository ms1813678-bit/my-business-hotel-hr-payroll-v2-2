"""One synthetic observation. Unknown outcomes retain stage/profile via exit125."""
import argparse
from pathlib import Path
import sys

def main():
    parser=argparse.ArgumentParser()
    for name in ('watchdog','watchdog-pin','launcher','launcher-pin','profile','stage','manifest-pin','product-root'):
        parser.add_argument('--'+name,required=True)
    args=parser.parse_args()
    sys.path.insert(0,str(Path(args.product_root).resolve()/'verification/windows_integrated_candidate'))
    sys.path.insert(0,str(Path(args.product_root).resolve()))
    sys.path.insert(0,str(Path(__file__).resolve().parent))
    from diagnostic_caller import observe_once, CleanupUnconfirmed
    from app.fingerprint_bounded_parse import _decode_worker
    try:
        result=observe_once(watchdog=args.watchdog,watchdog_pin=args.watchdog_pin,
            launcher=args.launcher,launcher_pin=args.launcher_pin,profile=args.profile,
            staged_root=args.stage,manifest_pin=args.manifest_pin,decoder=_decode_worker)
        mapping={'returned':0,'refused':122,'output_refused':123,'decode_refused':123,'timeout':124,'unavailable':126}
        if type(result) is not dict or result.get('cleanup_confirmed') is not True:
            raise CleanupUnconfirmed('Missing trusted cleanup result')
        code=mapping.get(result.get('status'),125)
        if code==0 and result.get('decoded') is not True:code=123
        print('diag_nodebug_result status=%s decoded=%d cleanup_confirmed=%d not_acceptance=1' %
              (result.get('status') if result.get('status') in mapping else 'unknown',
               int(result.get('decoded') is True),int(code!=125)),flush=True)
        return code
    except BaseException:
        print('diag_nodebug_result status=cleanup_unconfirmed decoded=0 cleanup_confirmed=0 not_acceptance=1',flush=True)
        return 125

if __name__=='__main__':
    try:code=main()
    except BaseException:code=125
    sys.exit(code)
