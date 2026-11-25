"""NEW disposable native stage/compile fixture. Synthetic inputs are NEVER RUN."""
import hashlib,importlib.util,json,os,pathlib,shutil,subprocess,sys
here=pathlib.Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('stage',here/'stager_candidate/staging/hony_stagetree.py')
s=importlib.util.module_from_spec(spec);spec.loader.exec_module(s)
if sys.platform!='win32' or os.environ.get('GITHUB_ACTIONS')!='true':raise SystemExit('disposable Windows CI only')
w=pathlib.Path(os.environ['RUNNER_TEMP'])/'hony-native-stager-fixture'
if w.exists():raise SystemExit('fresh work root required')
w.mkdir();src=w/'src';src.mkdir()
m=json.loads((here/'stage-source-manifest.json').read_bytes())
for e in m['files']:
 b=subprocess.check_output(['git','cat-file','blob','HEAD:'+e['path']])
 if hashlib.sha256(b).hexdigest()!=e['sha256']:raise SystemExit('committed app source hash mismatch')
 p=src/e['path'];p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b)
for p in ['app/.ENV','app/.Git/config','app/VAR/secret.txt','app/.env.example','app/.ENV.EXAMPLE']:
 try:s.check_source_relpath(p)
 except s.StageError:pass
 else:raise SystemExit('native refusal regression: '+p)
wh=w/'wheelhouse';wh.mkdir();payload=b'SYNTHETIC_NOT_A_WHEEL_NEVER_INSTALL';(wh/'alpha-1.0-py3-none-any.whl').write_bytes(payload)
req=w/'requirements.txt';req.write_bytes(('alpha==1.0 --hash=sha256:'+hashlib.sha256(payload).hexdigest()+'\n').encode())
inputs=w/'inputs';inputs.mkdir();vendor=b'SYNTHETIC_NOT_EXECUTABLE_NEVER_RUN';(inputs/'python-fixture.exe').write_bytes(vendor)
inv=w/'inventory.json';inv.write_text(json.dumps({'schema':s.INVENTORY_FORMAT,'version':1,'runtimes':[{'key':'python','filename':'python-fixture.exe','sha256':hashlib.sha256(vendor).hexdigest(),'silent_args':'/fixture-never-run','ok_exit_codes':[0]}]}))
stage=w/'staged';result=s.stage_tree(src,here/'stage-source-manifest.json',wh,req,inv,inputs,stage,'2.2.0')
assert result['status']=='staged';report=s.verify_tree(stage);assert report['status']=='pass'
print('NATIVE_ACTUAL285_STAGE_VERIFY_PASS SYNTHETIC_WHEEL_VENDOR NEVER_INSTALL_NEVER_RUN',flush=True)
compiler=pathlib.Path(os.environ['ProgramFiles(x86)'])/'Inno Setup 6/ISCC.exe'
if not compiler.is_file():raise SystemExit('preinstalled compiler required')
print('STAGER_ISCC_SHA256',hashlib.sha256(compiler.read_bytes()).hexdigest(),flush=True)
subprocess.run([str(compiler),'/Q','/DStageRoot='+str(stage/'inno'),'/O'+str(w/'compile-output'),str(here/'stager_candidate/HOnY-Desktop-Staged.iss')],check=True)
print('NATIVE_STAGED_ISS_COMPILE_PASS FIXTURE_OUTPUT_NEVER_RUN_NEVER_UPLOAD',flush=True)
