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
    stages={'cli_import','package_preflight','request','direct_oracle','integrity_holds','watchdog_launch','watchdog_started','watchdog_wait','watchdog_exit','decode'}
    stage='observe_entry'
    launch_began='0'
    native_exit='none'
    def progress(value, observed_exit=None):
        nonlocal stage,launch_began,native_exit
        stage=value if value in stages else 'unknown'
        if stage=='watchdog_launch':launch_began='unknown'
        elif stage=='watchdog_started':launch_began='1'
        if stage=='watchdog_exit' and type(observed_exit) is int and -2147483648<=observed_exit<=4294967295:
            native_exit=str(observed_exit)
        print('diag_nodebug_stage stage='+stage+' launch_began='+launch_began+' native_exit='+native_exit+' not_acceptance=1',flush=True)
    try:
        progress('cli_import')
        from diagnostic_caller import observe_once, CleanupUnconfirmed
        from app.fingerprint_bounded_parse import _decode_worker
        result=observe_once(watchdog=args.watchdog,watchdog_pin=args.watchdog_pin,
            launcher=args.launcher,launcher_pin=args.launcher_pin,profile=args.profile,
            staged_root=args.stage,manifest_pin=args.manifest_pin,decoder=_decode_worker,progress=progress)
        mapping={'returned':0,'refused':122,'output_refused':123,'decode_refused':123,'timeout':124,'unavailable':126}
        if type(result) is not dict or result.get('cleanup_confirmed') is not True:
            raise CleanupUnconfirmed('Missing trusted cleanup result')
        code=mapping.get(result.get('status'),125)
        if code==0 and result.get('decoded') is not True:code=123
        print('diag_nodebug_result status=%s decoded=%d cleanup_confirmed=%d not_acceptance=1' %
              (result.get('status') if result.get('status') in mapping else 'unknown',
               int(result.get('decoded') is True),int(code!=125)),flush=True)
        return code
    except BaseException as exc:
        kind={ModuleNotFoundError:'module_missing',ImportError:'import_refused',ValueError:'value_refused',OSError:'os_refused'}.get(type(exc),'other')
        if type(exc).__name__=='CleanupUnconfirmed' and type(exc).__module__=='diagnostic_caller':kind='cleanup_unconfirmed'
        print('diag_nodebug_failure stage='+stage+' kind='+kind+' launch_began='+launch_began+' native_exit='+native_exit+' not_acceptance=1',flush=True)
        print('diag_nodebug_result status=cleanup_unconfirmed decoded=0 cleanup_confirmed=0 not_acceptance=1',flush=True)
        return 125

if __name__=='__main__':
    try:code=main()
    except BaseException:code=125
    sys.exit(code)
