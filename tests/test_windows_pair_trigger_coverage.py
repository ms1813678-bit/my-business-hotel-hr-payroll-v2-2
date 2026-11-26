"""Every pinned runtime input must trigger both isolated qualification jobs."""
import fnmatch
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMES = ('windows-integrated-candidate.yml', 'windows-integrated-candidate-ifeo-off.yml')


def workflow_relative_path(path, root):
    return path.relative_to(root).as_posix()


def paths(name):
    text = (ROOT / '.github/workflows' / name).read_text()
    block = text.split('    paths:\n', 1)[1].split('permissions:', 1)[0]
    return [line.strip()[2:] for line in block.splitlines() if line.strip().startswith('- ')]


def test_both_workflows_cover_all_pinned_inputs_and_preserve_narrow_trigger():
    inputs = {'app/__init__.py'}
    for manifest in ('SOURCE-SHA256.json', 'PRODUCT-SHA256.json'):
        p = ROOT / 'verification/windows_integrated_candidate' / manifest
        inputs.update(json.loads(p.read_text()))
        inputs.add(workflow_relative_path(p, ROOT))
    for name in NAMES:
        filters = paths(name)
        assert filters == ['.github/workflows/' + name, 'verification/windows_integrated_candidate/**',
                           'app/attendance_import.py', 'app/fingerprint_bounded_parse.py',
                           'app/fingerprint_evidence.py', 'app/__init__.py']
        for source in inputs:
            assert any(fnmatch.fnmatchcase(source, pattern) for pattern in filters), (name, source)
        assert not any(fnmatch.fnmatchcase('docs/unrelated.md', pattern) for pattern in filters)
        assert not any(fnmatch.fnmatchcase('app/payroll.py', pattern) for pattern in filters)
        text = (ROOT / '.github/workflows' / name).read_text()
        assert 'branches: [ui-overhaul]' in text and 'timeout-minutes: 15' in text
        assert 'permissions:\n  contents: read' in text


def test_workflow_manifest_paths_use_posix_separators_on_every_host():
    from pathlib import PureWindowsPath
    path=PureWindowsPath('verification/windows_integrated_candidate/SOURCE-SHA256.json')
    root=PureWindowsPath('C:/repo')
    normalized=workflow_relative_path(root/path,root)
    assert normalized=='verification/windows_integrated_candidate/SOURCE-SHA256.json'
    assert any(fnmatch.fnmatchcase(normalized,p) for p in paths(NAMES[0]))
    assert not any(fnmatch.fnmatchcase(str(path),p) for p in paths(NAMES[0]))
