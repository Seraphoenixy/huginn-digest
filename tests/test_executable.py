import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout, closing
from pathlib import Path
from unittest.mock import patch

from config import ROOT, Settings
from interaction.cli import main
from integration.media import system_media_env, system_media_environment


class ExecutableTests(unittest.TestCase):
    def test_web_configuration_loads_without_dotenv(self):
        import builtins
        import uvicorn
        original_import = builtins.__import__
        def without_dotenv(name, *args, **kwargs):
            if name == 'dotenv' or name.startswith('dotenv.'):
                raise ModuleNotFoundError('dotenv intentionally unavailable')
            return original_import(name, *args, **kwargs)
        def load_web(app, **kwargs):
            server = uvicorn.Config(app, **kwargs)
            server.load()
            self.assertTrue(server.loaded)
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = self.config(directory)
            with patch('builtins.__import__', side_effect=without_dotenv), patch('uvicorn.run', side_effect=load_web):
                self.assertEqual(main(['--config', str(path), 'web']), 0)

    def config(self, directory):
        path = Path(directory) / 'config.toml'
        path.write_text((ROOT / 'config.example.toml').read_text(encoding='utf-8'), encoding='utf-8')
        return path

    def test_config_and_data_outside_resources_and_cwd(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            path = self.config(root)
            path.write_text(path.read_text(encoding='utf-8').replace('cookie_file = ""', 'cookie_file = "data/bili-cookie.txt"'), encoding='utf-8')
            with patch('config.APP_ROOT', root):
                settings = Settings()
            self.assertEqual(settings.config_file, path)
            self.assertEqual(settings.data_dir, root / 'data')
            self.assertEqual(settings.cookie_file, str(root / 'data/bili-cookie.txt'))
            self.assertEqual(settings.web_host, '127.0.0.1')
            self.assertEqual(settings.web_port, 8000)

    def test_worker_once_real_service_stays_offline(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = self.config(directory)
            with patch('requests.sessions.Session.request', side_effect=AssertionError('unexpected network')):
                with redirect_stdout(io.StringIO()), patch('integration.media.check_media_tools'):
                    self.assertEqual(main(['--config', str(path), 'worker', '--once']), 0)
            import sqlite3
            with closing(sqlite3.connect(Path(directory) / 'data' / 'daily.sqlite')) as db:
                self.assertEqual(db.execute('SELECT count(*) FROM tasks').fetchone()[0], 0)

    def test_admin_setup_uses_selected_external_config(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = self.config(directory)
            with patch('interaction.cli.getpass.getpass', side_effect=['abcdefghij', 'abcdefghij']):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(['--config', str(path), 'setup-admin']), 0)
            self.assertNotIn('abcdefghij', output.getvalue())
            self.assertTrue(Settings(config_file=path).admin_hash)

    def test_web_uses_configured_host_and_port(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            path = self.config(directory)
            path.write_text(path.read_text(encoding='utf-8').replace('port = 8000', 'port = 8123'), encoding='utf-8')
            with patch('uvicorn.run') as run:
                self.assertEqual(main(['--config', str(path), 'web']), 0)
            self.assertEqual(run.call_args.kwargs['port'], 8123)
            self.assertFalse(run.call_args.kwargs['access_log'])
            self.assertEqual(run.call_args.kwargs['loop'], 'asyncio')

    def test_frozen_library_path_cleanup_is_scoped_and_restored(self):
        with patch('sys.frozen', True, create=True), patch('sys.platform', 'linux'):
            with patch.dict(os.environ, {'LD_LIBRARY_PATH': '/bundle', 'LD_LIBRARY_PATH_ORIG': '/system'}, clear=True):
                self.assertEqual(system_media_env()['LD_LIBRARY_PATH'], '/system')
                self.assertEqual(os.environ['LD_LIBRARY_PATH'], '/bundle')
                with self.assertRaises(RuntimeError):
                    with system_media_environment():
                        self.assertEqual(os.environ['LD_LIBRARY_PATH'], '/system')
                        raise RuntimeError('fixture')
                self.assertEqual(os.environ['LD_LIBRARY_PATH'], '/bundle')
            with patch.dict(os.environ, {'LD_LIBRARY_PATH': '/bundle'}, clear=True):
                self.assertNotIn('LD_LIBRARY_PATH', system_media_env())

    def test_errors_never_echo_configuration_or_credentials(self):
        with patch('interaction.cli.Settings', side_effect=ValueError('private-credential')):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['worker']), 1)
            self.assertNotIn('private-credential', output.getvalue())
