"""Fixed synthetic trusted-side qualifier. Never a product entrypoint."""
import argparse
import base64
from datetime import datetime
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent))
from candidate_parent import parse_candidate

p=argparse.ArgumentParser()
for name in ('launcher','profile','stage','manifest-pin','launcher-pin','request','product-root'):
    p.add_argument('--'+name,required=True)
a=p.parse_args()
import hashlib
root=Path(a.product_root).resolve()
pins=json.loads(Path(__file__).with_name('PRODUCT-SHA256.json').read_text())
for path,pin in pins.items():
    if hashlib.sha256((root/path).read_bytes()).hexdigest()!=pin:
        raise ValueError('Product boundary pin mismatch')
sys.path.insert(0,str(root))
# Request is a fixed pre-generated synthetic fixture. Native transport uses stdin.
request=Path(a.request).read_bytes()
first,last,events,issues=parse_candidate(a.launcher,a.profile,a.stage,a.manifest_pin,a.launcher_pin,request,{'SYN':1})
if len(events)!=4 or events[0]['employee_id'] is not None:raise ValueError('Occurrence/unknown mismatch')
if not events[1]['punch_at']==events[2]['punch_at']==datetime(2026,11,1,23):raise ValueError('Duplicate mismatch')
if events[3]['punch_at']!=datetime(2026,11,2,7):raise ValueError('Overnight mismatch')
print(json.dumps({'contained_real_parser':'workload-pass','occurrences':len(events),
                  'duplicate_unknown_overnight':True,'security_acceptance':'NOT_ESTABLISHED'},sort_keys=True))
