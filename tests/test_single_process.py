import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from config import ROOT, Settings
from interaction.cli import main
from interaction.web import create_app
from integration.lock import worker_lock
from orchestration.service import DailyService


class SingleProcessTests(unittest.TestCase):
    def settings(self, directory):
        path = Path(directory) / 'config.toml'
        path.write_text((ROOT / 'config.example.toml').read_text(encoding='utf-8'), encoding='utf-8')
        return Settings(config_file=path, data_dir=Path(directory) / 'data')

    def test_default_and_serve_start_combined_app(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            for command in ([], ['serve'], ['web']):
                with patch('interaction.cli.Settings', return_value=settings), patch('uvicorn.run') as run:
                    self.assertEqual(main(command), 0)
                    self.assertIsInstance(run.call_args.args[0].state.service, DailyService)

    def test_worker_runs_in_same_process_without_blocking_web_and_joins(self):
        entered = threading.Event()
        release = threading.Event()
        identities = []
        threads = []
        def tick(service):
            identities.append((os.getpid(), threading.get_ident()))
            threads.append(threading.current_thread())
            service.store.set_meta('heartbeat', 'offline-fixture')
            entered.set()
            if not release.wait(5):
                raise AssertionError('test failed to release worker')
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            with patch('integration.media.check_media_tools'), patch.object(DailyService, 'tick', tick):
                app = create_app(settings)
                try:
                    with TestClient(app) as client:
                        self.assertTrue(entered.wait(5))
                        self.assertEqual(client.get('/health').status_code, 200)
                        self.assertEqual(app.state.service.store.meta('heartbeat'), 'offline-fixture')
                        self.assertEqual(identities[0][0], os.getpid())
                        self.assertNotEqual(identities[0][1], threading.get_ident())
                        self.assertFalse(threads[0].daemon)
                        release.set()
                finally:
                    release.set()
                self.assertFalse(threads[0].is_alive())
                # Shutdown releases the production lock for the next start.
                with worker_lock(settings.data_dir / 'worker.lock'):
                    pass

    def test_duplicate_worker_fails_startup_before_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = self.settings(directory)
            with worker_lock(settings.data_dir / 'worker.lock'):
                with patch('integration.media.check_media_tools'), patch('integration.store.Store.recover') as recover:
                    with self.assertRaisesRegex(RuntimeError, '后台启动失败'):
                        with TestClient(create_app(settings)):
                            pass
                    recover.assert_not_called()

    def test_idle_shutdown_interrupts_wait(self):
        done = threading.Event()
        with tempfile.TemporaryDirectory() as directory:
            service = DailyService(self.settings(directory))
            with patch('integration.media.check_media_tools'), patch.object(service, 'tick', side_effect=done.set):
                with service.background_worker():
                    self.assertTrue(done.wait(5))
                self.assertFalse(any(t.name == 'daily-worker' and t.is_alive() for t in threading.enumerate()))
