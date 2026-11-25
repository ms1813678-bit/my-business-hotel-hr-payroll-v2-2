"""Model/endpoint setting sources for the local assistant. Pure unit tests: no server, no model."""
import logging
import os
import tempfile
import unittest

from app import ai_local_config as cfg


class LocalConfigTests(unittest.TestCase):
    def _env_file(self, text):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = os.path.join(d.name, ".env")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_default_when_nothing_set(self):
        self.assertEqual(cfg.resolve_model({}, "/nonexistent/.env"), cfg.DEFAULT_MODEL)
        self.assertEqual(cfg.resolve_base_url({}, "/nonexistent/.env"), "http://127.0.0.1:11434")

    def test_env_file_is_read(self):
        p = self._env_file("LOCAL_AI_MODEL=qwen2.5:7b\nLOCAL_AI_BASE_URL=http://localhost:11434/\n")
        self.assertEqual(cfg.resolve_model({}, p), "qwen2.5:7b")
        self.assertEqual(cfg.resolve_base_url({}, p), "http://localhost:11434")

    def test_environment_beats_env_file(self):
        p = self._env_file("LOCAL_AI_MODEL=qwen2.5:7b\n")
        self.assertEqual(cfg.resolve_model({"LOCAL_AI_MODEL": "llama3.2:3b"}, p), "llama3.2:3b")

    def test_documented_alias_works_and_primary_name_wins(self):
        self.assertEqual(cfg.resolve_model({"OLLAMA_MODEL": "qwen2.5:7b"}, "/x/.env"), "qwen2.5:7b")
        self.assertEqual(
            cfg.resolve_model({"OLLAMA_MODEL": "a:1", "LOCAL_AI_MODEL": "b:2"}, "/x/.env"), "b:2"
        )

    def test_blank_value_means_default(self):
        self.assertEqual(cfg.resolve_model({"LOCAL_AI_MODEL": "   "}, "/x/.env"), cfg.DEFAULT_MODEL)

    def test_invalid_names_fall_back_and_warn(self):
        for bad in ("qwen 2.5", "a;b", "x\ny", "-lead", "a" * 200, "../etc"):
            with self.subTest(bad=bad):
                with self.assertLogs(cfg._log, level=logging.WARNING):
                    self.assertEqual(cfg.resolve_model({"LOCAL_AI_MODEL": bad}, "/x/.env"), cfg.DEFAULT_MODEL)

    def test_valid_ollama_tag_shapes(self):
        for ok in ("qwen2.5:3b", "library/qwen2.5:7b-instruct-q4_K_M", "name@sha256:abc123"):
            self.assertEqual(cfg.resolve_model({"LOCAL_AI_MODEL": ok}, "/x/.env"), ok)

    def test_unreadable_env_file_is_not_fatal(self):
        self.assertEqual(cfg.resolve_model({}, tempfile.gettempdir()), cfg.DEFAULT_MODEL)

    def test_env_file_url_still_goes_through_loopback_check(self):
        from app import ai_assistant as ai

        old = ai.OLLAMA_BASE_URL
        self.addCleanup(setattr, ai, "OLLAMA_BASE_URL", old)
        ai.OLLAMA_BASE_URL = cfg.resolve_base_url({"LOCAL_AI_BASE_URL": "http://example.com:11434"}, "/x/.env")
        with self.assertRaises(RuntimeError):
            ai._ensure_loopback()

    def test_assistant_uses_resolved_values(self):
        from app import ai_assistant as ai

        self.assertEqual(ai.OLLAMA_MODEL, cfg.resolve_model())
        self.assertEqual(ai.OLLAMA_BASE_URL, cfg.resolve_base_url())


if __name__ == "__main__":
    unittest.main()


class DefaultPathTests(unittest.TestCase):
    def test_default_dotenv_is_install_folder_not_working_directory(self):
        from pathlib import Path

        self.assertEqual(Path(cfg.DEFAULT_DOTENV).name, ".env")
        self.assertTrue((Path(cfg.DEFAULT_DOTENV).parent / "app" / "ai_local_config.py").exists())

    def test_quoted_values_and_utf8_no_bom_like_setup_writes(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        path = os.path.join(d.name, ".env")
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write('APP_NAME="M.Y. Business Hotel HR"\r\nLOCAL_AI_MODEL="qwen2.5:7b"\r\n')
        self.assertEqual(cfg.resolve_model({}, path), "qwen2.5:7b")


class WindowsEnvTests(unittest.TestCase):
    def test_bom_first_key_is_not_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, '.env')
            with open(path, 'w', encoding='utf-8-sig') as stream:
                stream.write('LOCAL_AI_MODEL=qwen2.5:7b\n')
            self.assertEqual(cfg.resolve_model({}, path), 'qwen2.5:7b')

    def test_environment_alias_beats_file_primary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, '.env')
            with open(path, 'w', encoding='utf-8') as stream:
                stream.write('LOCAL_AI_MODEL=qwen2.5:7b\n')
            self.assertEqual(cfg.resolve_model({'OLLAMA_MODEL':'custom:1'}, path), 'custom:1')


class ReaderFailureTests(unittest.TestCase):
    def test_missing_reader_warns_without_secret_values(self):
        from unittest.mock import patch
        with patch.dict('sys.modules', {'dotenv': None}), self.assertLogs(cfg._log, level='WARNING') as logs:
            self.assertEqual(cfg.resolve_model({'LOCAL_AI_MODEL':'custom:1'}, '/missing'), 'custom:1')
        self.assertIn('reader is unavailable', logs.output[0])
        self.assertNotIn('custom:1', logs.output[0])

    def test_unreadable_file_warns_and_preserves_environment(self):
        with tempfile.TemporaryDirectory() as directory, self.assertLogs(cfg._log, level='WARNING'):
            self.assertEqual(cfg.resolve_model({'LOCAL_AI_MODEL':'custom:1'}, directory), 'custom:1')
