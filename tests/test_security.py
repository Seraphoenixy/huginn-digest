import contextlib
import io
import re
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient
from config import Settings
from integration.auth import hash_password
from integration.http.public import PublicHTTPConnection, PublicHTTPSConnection, PublicAdapter, public_url
from integration.http.llm import complete, CloudError
from integration.http.siliconflow_asr import transcribe_audio
from interaction.web import create_app
from orchestration.service import DailyService


class SecurityTests(unittest.TestCase):
    def test_dns_rebinding_blocked_at_connection(self):
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]
        with patch('socket.getaddrinfo', side_effect=[public, private]), patch('socket.socket') as connect:
            public_url('https://example.com')
            with self.assertRaises(ValueError):
                PublicHTTPSConnection('example.com', timeout=3)._new_conn()
            connect.assert_not_called()

    def test_connection_uses_checked_address_without_resolving_again(self):
        for cls in (PublicHTTPConnection, PublicHTTPSConnection):
            address = ('93.184.216.34', 443)
            with patch('socket.getaddrinfo', return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, '', address)]) as resolve, patch('socket.socket') as sock:
                conn = cls('example.com', port=443, timeout=3)
                self.assertIs(conn._new_conn(), sock.return_value)
                resolve.assert_called_once()
                sock.return_value.connect.assert_called_once_with(address)
                self.assertEqual(conn.host, 'example.com')
        adapter = PublicAdapter()
        self.assertIs(adapter.poolmanager.connection_from_url('https://example.com').ConnectionCls, PublicHTTPSConnection)
        adapter.close()

    def test_logout_revokes_copied_cookie_across_service_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), session_secret='security-fixture-secret-at-least-32-characters', admin_hash=hash_password('fixture-password'), secure_cookie=False)
            app = create_app(settings, run_background=False)
            with TestClient(app) as client:
                page = client.get('/admin/login')
                csrf = re.search('name="token" value="([^"]+)"', page.text)[1]
                client.post('/admin/login', data={'password': 'fixture-password', 'token': csrf})
                stolen = client.cookies.get('daily_session')
                csrf = app.state.service.session(stolen)['csrf']
                self.assertEqual(client.post('/admin/logout', data={'token': csrf}).status_code, 200)
                client.cookies.set('daily_session', stolen)
                self.assertEqual(client.get('/admin').status_code, 401)
                self.assertFalse(DailyService(settings).session(stolen))
                stored = str(app.state.service.store.query("SELECT * FROM meta WHERE key LIKE 'revoked:%'"))
                self.assertNotIn(stolen, stored)

    def test_cloud_keys_not_sent_to_http_or_redirect_targets(self):
        settings = Mock(llm_key='offline-key')
        settings.get.side_effect = lambda section, key, default=None: {'base_url':'http://example.com/v1','model':'fixture'}.get(key, default)
        with patch('integration.http.llm.requests.post') as post:
            with self.assertRaises(CloudError):complete(settings, [])
            post.assert_not_called()
        settings.get.side_effect = lambda section, key, default=None: {'base_url':'https://example.com/v1','model':'fixture'}.get(key, default)
        response = Mock(status_code=307)
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        with patch('integration.http.llm.requests.post', return_value=response) as post:
            with self.assertRaises(CloudError):complete(settings, [])
            self.assertFalse(post.call_args.kwargs['allow_redirects'])
            post.assert_called_once()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'audio.wav'
            path.write_bytes(b'offline')
            with patch('integration.http.siliconflow_asr.requests.post', return_value=Mock(status_code=307)) as post:
                with self.assertRaises(RuntimeError):transcribe_audio(path, api_key='offline-key', model='fixture', timeout=1)
                self.assertFalse(post.call_args.kwargs['allow_redirects'])
                post.assert_called_once()

    def test_bili_errors_do_not_print_private_response(self):
        from integration.sources.bili import BiliVideoSource
        with tempfile.TemporaryDirectory() as directory:
            source = BiliVideoSource(download_cache_dir=directory)
            output = io.StringIO()
            with patch.object(source, '_ensure_device_cookies'), patch.object(source.session, 'get', side_effect=RuntimeError('private-cookie-value')), contextlib.redirect_stdout(output):
                self.assertIsNone(source.get_video_info('BV1xx411c7mD'))
            self.assertNotIn('private-cookie-value', output.getvalue())
            source.session.close()

    def test_parallel_login_failures_respect_limit(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as directory:
            service = DailyService(Settings(data_dir=Path(directory), session_secret='security-fixture-secret-at-least-32-characters', admin_hash='offline-hash'))
            with patch('orchestration.service.verify_password', return_value=False) as verify:
                with ThreadPoolExecutor(max_workers=12) as workers:
                    self.assertFalse(any(workers.map(lambda _: service.login('bad-password', 'fixture-ip'), range(12))))
                self.assertEqual(verify.call_count, 5)

    def test_public_routes_do_not_expose_credentials_or_files(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(data_dir=Path(directory), asr_key='private-asr-fixture', llm_key='private-llm-fixture', session_secret='security-fixture-secret-at-least-32-characters')
            app = create_app(settings, run_background=False)
            with TestClient(app) as client:
                for url in ('/', '/health', '/api/public/preview', '/api/public/days', '/config.toml', '/data/daily.sqlite', '/data/bili-cookie.txt', '/static/../config.toml', '/admin'):
                    response = client.get(url)
                    for secret in (settings.asr_key, settings.llm_key, settings.session_secret):
                        self.assertNotIn(secret, response.text)
                    if url.startswith(('/config', '/data')):
                        self.assertEqual(response.status_code, 404)
                self.assertEqual(client.get('/admin').status_code, 401)

    def test_server_does_not_trust_forwarded_ip_headers(self):
        from config import ROOT
        from interaction.cli import main
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory)/'config.toml'
            config.write_text((ROOT/'config.example.toml').read_text(encoding='utf-8'), encoding='utf-8')
            with patch('uvicorn.run') as run:
                self.assertEqual(main(['--config', str(config), 'web']), 0)
                self.assertFalse(run.call_args.kwargs['proxy_headers'])
