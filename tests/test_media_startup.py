import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from config import ROOT, Settings
from integration.media import check_media_tools, MediaToolsError
from interaction.cli import main
from interaction.web import create_app
from fastapi.testclient import TestClient
from orchestration.service import DailyService


class MediaStartupTests(unittest.TestCase):
    def test_both_tools_must_execute_successfully(self):
        def run(command, **kwargs):
            self.assertEqual(command[1:], ['-version'])
            self.assertEqual(kwargs['timeout'], 10)
            self.assertIn('env', kwargs)
            return Mock(returncode=0, stdout=(command[0] + ' version fixture').encode())
        with patch('integration.media.subprocess.run', side_effect=run) as process:
            check_media_tools()
        self.assertEqual(process.call_count, 2)

    def test_missing_timeout_permissions_and_invalid_binary_are_redacted(self):
        cases = [FileNotFoundError('private-path'), PermissionError('private-path'),
                 subprocess.TimeoutExpired('private-command', 10),
                 Mock(returncode=1, stdout=b'private-output'),
                 Mock(returncode=0, stdout=b'unrelated-tool')]
        for failure in cases:
            with self.subTest(failure=type(failure).__name__):
                behavior = {'side_effect': failure} if isinstance(failure, Exception) else {'return_value': failure}
                with patch('integration.media.subprocess.run', **behavior):
                    with self.assertRaises(MediaToolsError) as error:
                        check_media_tools()
                message = str(error.exception)
                self.assertIn('ffmpeg', message)
                self.assertIn('ffprobe', message)
                self.assertIn('apt-get install ffmpeg', message)
                self.assertNotIn('private-', message)
                self.assertNotIn('unrelated-tool', message)

    def test_ffprobe_missing_even_when_ffmpeg_exists(self):
        with patch('integration.media.subprocess.run', side_effect=[
                Mock(returncode=0, stdout=b'ffmpeg version fixture'), FileNotFoundError()]):
            with self.assertRaises(MediaToolsError) as error:
                check_media_tools()
        self.assertIn('未找到ffprobe', str(error.exception))
        self.assertNotIn('未找到ffmpeg', str(error.exception))

    def test_worker_failure_returns_nonzero_before_task_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text((ROOT / 'config.example.toml').read_text(encoding='utf-8'), encoding='utf-8')
            settings = Settings(config_file=path, data_dir=Path(directory) / 'data')
            with patch('interaction.cli.Settings', return_value=settings):
                with patch('integration.media.subprocess.run', side_effect=FileNotFoundError()):
                    with patch.object(DailyService, 'tick') as tick, patch('integration.store.Store.recover') as recover:
                        with redirect_stdout(io.StringIO()) as output:
                            self.assertEqual(main(['worker', '--once']), 1)
                        tick.assert_not_called()
                        recover.assert_not_called()
            self.assertIn('后台启动失败', output.getvalue())
            self.assertIn('未找到ffmpeg', output.getvalue())
            self.assertIn('未找到ffprobe', output.getvalue())

    def test_web_startup_warns_but_keeps_reading_available(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory))
            with patch('integration.media.subprocess.run', side_effect=FileNotFoundError()):
                with redirect_stdout(io.StringIO()) as output:
                    with TestClient(create_app(settings)) as client:
                        self.assertEqual(client.get('/').status_code, 200)
                        self.assertEqual(client.get('/health').status_code, 200)
            self.assertIn('媒体依赖警告', output.getvalue())
            self.assertIn('公开阅读和管理仍可使用', output.getvalue())
