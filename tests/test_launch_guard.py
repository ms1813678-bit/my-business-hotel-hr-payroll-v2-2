"""Standalone launch guard: wrong working folder is refused with a plain Arabic message."""
import ast
import os
from pathlib import Path

import pytest

from app import launch_guard as lg


def make_root(tmp_path, env=True):
    root = tmp_path / "HotelHR"
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text("")
    (root / "pyproject.toml").write_text("")
    (root / "alembic.ini").write_text("")
    if env:
        (root / ".env").write_text("DATABASE_URL=not-read\n")
    return root


def test_ok_from_the_install_root(tmp_path):
    root = make_root(tmp_path)
    result = lg.check_launch_context(root, root, {})
    assert result.ok and result.code == lg.CODE_OK and result.message_ar == ""


def test_ok_when_cwd_is_given_with_dots_or_trailing_parts(tmp_path):
    root = make_root(tmp_path)
    assert lg.check_launch_context(root, root / "app" / "..", {}).ok


def test_other_folder_is_refused_even_if_it_has_its_own_env(tmp_path):
    root = make_root(tmp_path)
    other = tmp_path / "Other"
    other.mkdir()
    (other / ".env").write_text("DATABASE_URL=another\n")
    result = lg.check_launch_context(root, other, {})
    assert not result.ok and result.code == lg.CODE_WRONG_CWD
    assert "فولدر غلط" in result.message_ar


def test_subfolder_of_root_is_refused(tmp_path):
    root = make_root(tmp_path)
    assert lg.check_launch_context(root, root / "app", {}).code == lg.CODE_WRONG_CWD


def test_missing_env_is_refused_unless_database_url_is_in_the_environment(tmp_path):
    root = make_root(tmp_path, env=False)
    assert lg.check_launch_context(root, root, {}).code == lg.CODE_ENV_MISSING
    assert lg.check_launch_context(root, root, {"DATABASE_URL": "x"}).ok
    assert lg.check_launch_context(root, root, {}, require_env_file=False).ok


def test_env_that_is_a_folder_is_refused(tmp_path):
    root = make_root(tmp_path, env=False)
    (root / ".env").mkdir()
    assert lg.check_launch_context(root, root, {"DATABASE_URL": "x"}).code == lg.CODE_ENV_NOT_FILE


@pytest.mark.parametrize("missing", ["pyproject.toml", "alembic.ini", os.path.join("app", "__init__.py")])
def test_not_an_install_root_is_refused_first(tmp_path, missing):
    root = make_root(tmp_path)
    (root / missing).unlink()
    assert lg.check_launch_context(root, tmp_path, {}).code == lg.CODE_NOT_ROOT


def test_nonexistent_root_is_refused(tmp_path):
    assert lg.check_launch_context(tmp_path / "nope", tmp_path, {}).code == lg.CODE_NOT_ROOT


def test_symlinked_cwd_to_root_is_accepted_and_symlink_elsewhere_is_not(tmp_path):
    root = make_root(tmp_path)
    link = tmp_path / "link"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert lg.check_launch_context(root, link, {}).ok
    other = tmp_path / "Other"
    other.mkdir()
    bad = tmp_path / "badlink"
    bad.symlink_to(other, target_is_directory=True)
    assert lg.check_launch_context(root, bad, {}).code == lg.CODE_WRONG_CWD


def test_windows_case_and_slash_difference_is_the_same_folder(monkeypatch):
    import ntpath
    monkeypatch.setattr(lg.os, "name", "nt")
    monkeypatch.setattr(lg.os.path, "realpath", lambda p: ntpath.normpath(str(p)))
    assert lg._same_folder("C:\\HotelHR", "c:/hotelhr")
    assert not lg._same_folder("C:\\HotelHR", "C:\\HotelHR2")


def test_default_install_root_is_the_real_repo_root_and_real_cwd_is_checked():
    root = lg.default_install_root()
    assert (root / "app" / "launch_guard.py").is_file()
    assert lg.check_launch_context(root, root, {"DATABASE_URL": "x"}).ok


def test_never_reads_env_contents_and_is_not_imported_by_the_app(tmp_path):
    src = Path(lg.__file__).read_text(encoding="utf-8")
    assert "read_text" not in src and "open(" not in src
    for path in Path(lg.__file__).parent.glob("*.py"):
        if path.name == "launch_guard.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else (
                [node.module or ""] + [a.name for a in node.names] if isinstance(node, ast.ImportFrom) else [])
            assert not any("launch_guard" in n for n in names), path.name


def test_getcwd_failure_is_the_wrong_folder_refusal(tmp_path, monkeypatch):
    root = make_root(tmp_path)

    def boom():
        raise FileNotFoundError("cwd deleted")
    monkeypatch.setattr(lg.os, "getcwd", boom)
    result = lg.check_launch_context(root, None, {})
    assert not result.ok and result.code == lg.CODE_WRONG_CWD and result.message_ar


@pytest.mark.parametrize("bad", ["", "   ", 5, b"x"])
def test_empty_or_non_path_cwd_is_refused(tmp_path, bad):
    root = make_root(tmp_path)
    assert lg.check_launch_context(root, bad, {}).code == lg.CODE_WRONG_CWD


def test_every_arabic_message_has_no_latin_letters():
    import re
    assert lg._MESSAGES_AR and all(not re.search("[A-Za-z]", m) for m in lg._MESSAGES_AR.values())


def test_nul_byte_in_cwd_is_refused_not_raised(tmp_path):
    root = make_root(tmp_path)
    result = lg.check_launch_context(root, str(root) + "\x00evil", {})
    assert not result.ok and result.code == lg.CODE_WRONG_CWD


def test_getcwd_value_error_is_refused(tmp_path, monkeypatch):
    root = make_root(tmp_path)
    monkeypatch.setattr(lg.os, "getcwd", lambda: (_ for _ in ()).throw(ValueError("bad")))
    assert lg.check_launch_context(root, None, {}).code == lg.CODE_WRONG_CWD
