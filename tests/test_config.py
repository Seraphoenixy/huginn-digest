import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch
from config import ROOT, Settings
from orchestration.credentials import configure_credentials
from integration.auth import verify_password

class ConfigTests(unittest.TestCase):
    def test_env_file_is_not_loaded(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / 'config.toml'
            path.write_text((ROOT / 'config.example.toml').read_text(encoding='utf-8'), encoding='utf-8')
            (path.parent / '.env').write_text('SILICONFLOW_API_KEY=unused-fixture\nCOOKIE_SECURE=true\n', encoding='utf-8')
            settings = Settings(config_file=path)
            self.assertEqual(settings.asr_key, '')
            self.assertFalse(settings.secure_cookie)
            self.assertNotIn('SILICONFLOW_API_KEY', os.environ)

    def test_bili_refresh_config_validation_and_legacy_defaults(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = Path(directory) / 'config.toml'
            template = (ROOT / 'config.example.toml').read_text(encoding='utf-8')
            for assignment in ('auto_refresh = "yes"', 'refresh_check_seconds = 1', 'refresh_retry_seconds = true'):
                key = assignment.split(' = ')[0]
                lines = [assignment if line.startswith(key + ' = ') else line for line in template.splitlines()]
                path.write_text('\n'.join(lines), encoding='utf-8')
                with self.subTest(key=key), self.assertRaises(ValueError):
                    Settings(config_file=path, data_dir=Path(directory) / 'data')
            legacy = '\n'.join(line for line in template.splitlines() if not line.startswith(('auto_refresh = ', 'refresh_check_seconds = ', 'refresh_retry_seconds = ')))
            path.write_text(legacy, encoding='utf-8')
            settings = Settings(config_file=path, data_dir=Path(directory) / 'data')
            self.assertTrue(settings.bili_auto_refresh)
            self.assertEqual(settings.bili_refresh_check_seconds, 86400)
            self.assertFalse(Settings(config_file=path, data_dir=Path(directory) / 'data', bili_auto_refresh=False).bili_auto_refresh)

    def test_file_shared_by_both_processes_and_overrides(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{},clear=True):
            path=Path(directory)/'config.toml'
            text=(ROOT/'config.example.toml').read_text(encoding='utf-8')
            # Independently set ASR and LLM sections without printing fixture keys.
            text=text.replace('[asr]\napi_key = ""','[asr]\napi_key = "test-asr"').replace('[llm]\napi_key = ""','[llm]\napi_key = "test-llm"')
            path.write_text(text,encoding='utf-8')
            first=Settings(config_file=path,data_dir=Path(directory)/'data')
            second=Settings(config_file=path,data_dir=Path(directory)/'data')
            self.assertEqual(first.asr_key,second.asr_key)
            self.assertEqual(first.asr_key,'test-asr')
            self.assertEqual(first.llm_key,'test-llm')
            self.assertFalse(first.secure_cookie)
            with patch.dict(os.environ,{'SILICONFLOW_API_KEY':'env-asr','COOKIE_SECURE':'true'}):
                settings=Settings(config_file=path,data_dir=Path(directory)/'data')
                self.assertEqual(settings.asr_key,'env-asr')
                self.assertTrue(settings.secure_cookie)
                self.assertEqual(Settings(config_file=path,data_dir=Path(directory)/'data',asr_key='explicit').asr_key,'explicit')
            with patch.dict(os.environ,{'SILICONFLOW_API_KEY':''}):
                self.assertEqual(Settings(config_file=path,data_dir=Path(directory)/'data').asr_key,'test-asr')
            self.assertNotIn('test-asr',repr(first))

    def test_credentials_written_without_changing_other_config(self):
        with tempfile.TemporaryDirectory() as directory,patch.dict(os.environ,{},clear=True):
            path=Path(directory)/'config.toml'
            path.write_text((ROOT/'config.example.toml').read_text(encoding='utf-8'),encoding='utf-8')
            settings=Settings(config_file=path,data_dir=Path(directory)/'data')
            original=tomllib.loads(path.read_text(encoding='utf-8'))
            configure_credentials(settings,'abcdefghij')
            result=Settings(config_file=path,data_dir=Path(directory)/'data')
            after=tomllib.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(original['asr'],after['asr'])
            self.assertEqual(original['llm'],after['llm'])
            self.assertEqual(original['security']['secure_cookie'],after['security']['secure_cookie'])
            self.assertTrue(verify_password('abcdefghij',result.admin_hash))
            self.assertGreaterEqual(len(result.session_secret),32)
            self.assertNotIn('abcdefghij',path.read_text(encoding='utf-8'))
            configure_credentials(result,'klmnopqrst')
            updated=Settings(config_file=path,data_dir=Path(directory)/'data')
            self.assertTrue(verify_password('klmnopqrst',updated.admin_hash))
