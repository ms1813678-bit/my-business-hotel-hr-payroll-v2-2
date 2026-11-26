"""Decode the probe exit code into per-name success bits. Parsed from the existing capture line only."""
import argparse
import json
from pathlib import Path
import re
import sys


def decode(items,code):
    if type(code) is not int or not 0x20000000<=code<=0x2FFFFFFF:raise ValueError('Not a probe exit code')
    mask=code&0x0FFFFFFF
    if mask>>len(items):raise ValueError('Mask has bits beyond the probe list')
    return [{'bit':i['bit'],'name':i['name'],'class':i.get('class','unknown'),'known_dll':i.get('known_dll',0),'loaded':int(bool(mask>>i['bit']&1))} for i in items]


def main():
    p=argparse.ArgumentParser();p.add_argument('--map',required=True);p.add_argument('--exit',required=True)
    a=p.parse_args()
    if not re.fullmatch(r'[0-9]{1,10}',a.exit):raise ValueError('Exit code format')
    items=json.loads(Path(a.map).read_text())
    rows=decode(items,int(a.exit))
    for r in rows:print('probe_result bit=%d name=%s class=%s known_dll=%d loaded_by_LoadLibraryEx_DONT_RESOLVE=%d'%(r['bit'],r['name'],r['class'],r['known_dll'],r['loaded']))
    print('probe_limits dont_resolve_maps_one_image_without_imports_or_DllMain=1 system32_dependencies_not_recursed=1 app_dir_shadowing_not_tested=1 known_dll_bits_say_nothing_about_acls=1 failure_reason_not_recorded=1')
    print('probe_loaded=%d probe_total=%d diagnostic_only=1 negative_inconclusive=1 security_acceptance=NOT_ESTABLISHED'%(sum(r['loaded'] for r in rows),len(rows)))


if __name__=='__main__':
    try:main()
    except (ValueError,OSError) as exc:
        print('probe_decode_refused '+str(exc)[:80]);sys.exit(2)
