"""NEW disposable native offline wheel closure, never publishes binary artifacts."""
import hashlib, importlib.util, json, os, pathlib, subprocess, sys, venv
here = pathlib.Path(__file__).resolve().parent
if sys.platform != 'win32' or os.environ.get('GITHUB_ACTIONS') != 'true':
    raise SystemExit('disposable Windows CI only')
work = pathlib.Path(os.environ['RUNNER_TEMP']) / 'hony-offline-wheel-closure'
if work.exists():
    raise SystemExit('fresh work root required')
work.mkdir()
wh = work / 'wheels'
wh.mkdir()
# Inputs are all pinned upstream wheel hashes except proxy-tools, whose pinned
# upstream sdist is built using the already hash-pinned build-tool environment.
subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--disable-pip-version-check',
                '--no-deps', '--require-hashes', '--no-build-isolation',
                '--wheel-dir', str(wh), '-r',
                str(here / 'requirements-native-SDIST-NEW-CANDIDATE.txt')], check=True)
spec = importlib.util.spec_from_file_location('stage', here / 'stager_candidate/staging/hony_stage.py')
s = importlib.util.module_from_spec(spec)
spec.loader.exec_module(s)
reqs = {}
lines = []
for p in sorted(wh.iterdir()):
    if not p.is_file() or p.suffix != '.whl' or p.is_symlink():
        raise SystemExit('unexpected wheel output')
    name, version = s.wheel_pin(p.name)
    if name in reqs:
        raise SystemExit('duplicate wheel output')
    h = hashlib.sha256(p.read_bytes()).hexdigest()
    reqs[name] = {'version': version, 'sha256': h}
    lines.append(name + '==' + version + ' --hash=sha256:' + h)
# Closure must retain every exact pinned name/version, not silently drop a wheel.
source = s.parse_hashed_requirements((here / 'requirements-native-SDIST-NEW-CANDIDATE.txt').read_text())
assert set(reqs) == set(source)
assert all(reqs[n]['version'] == source[n]['version'] for n in source)
for n in source:
    if n != 'proxy-tools':
        assert reqs[n]['sha256'] == source[n]['sha256']
lock = s.build_wheels_lock(reqs, wh)
lockpath = work / 'wheels.lock.json'
lockpath.write_text(json.dumps(lock))
reqpath = work / 'offline-requirements.txt'
reqpath.write_text('\n'.join(lines) + '\n')
print('NATIVE_WHEELHOUSE_ORIGINS', json.dumps(lock, sort_keys=True), flush=True)
fresh = work / 'fresh-venv'
venv.EnvBuilder(with_pip=True).create(fresh)
py = fresh / 'Scripts/python.exe'
subprocess.run([str(py), '-m', 'pip', 'install', '--disable-pip-version-check',
                '--no-index', '--no-deps', '--only-binary=:all:', '--require-hashes',
                '--find-links', str(wh), '-r', str(reqpath)], check=True)
subprocess.run([str(py), '-m', 'pip', 'check'], check=True)
subprocess.run([str(py), '-I', str(here / 'stager_candidate/staging/verify_install.py'),
                str(lockpath)], check=True)
subprocess.run([str(py), '-I', str(here / 'dependency_smoke.py')], check=True)
print('NATIVE_ACTUAL50_OFFLINE_FRESH_INSTALL_METADATA_IMPORT_PASS NOT_BYTE_INTEGRITY_NOT_RELEASE', flush=True)
