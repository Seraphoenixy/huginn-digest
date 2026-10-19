import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import requests
from requests.adapters import BaseAdapter

from config import Settings, ROOT
from integration.bili_cookies import BiliSession
from integration.bili_refresh import BASE, BiliRefreshError, cookie_value, correspond_path
from integration.lock import worker_lock
from integration.sources.bili import BiliVideoSource, BiliSourceError, sanitize_cookie
from orchestration.service import DailyService


class PassportAdapter(BaseAdapter):
    """真实requests准备请求及提取响应Cookie，只替换网络传输。"""
    def __init__(self):
        self.calls = []
        self.refresh = True
        self.info_code = 0
        self.confirm_error = False
        self.rotate_error = False
        self.missing_cookies = False
        self.html = '<div id="1-name">one-time-csrf</div>'
        self.on_confirm = None

    def send(self, request, **kwargs):
        self.calls.append(request)
        response = requests.Response()
        response.status_code = 200
        response.request = request
        response.url = request.url
        response.cookies = requests.cookies.RequestsCookieJar()
        headers = Message()
        path = urlsplit(request.url).path
        if path.endswith('/cookie/info'):
            body = {'code': self.info_code, 'data': {'refresh': self.refresh, 'timestamp': 1700000000000}}
        elif '/correspond/1/' in path:
            response._content = self.html.encode()
            response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=headers))
            return response
        elif path.endswith('/cookie/refresh'):
            body = {'code': 86095 if self.rotate_error else 0,
                    'message': 'private-server-secret', 'data': {'status': 0, 'refresh_token': 'new-token'}}
            if not self.rotate_error and not self.missing_cookies:
                for name, value in [('SESSDATA', 'new-session'), ('bili_jct', 'new-csrf')]:
                    headers.add_header('Set-Cookie', f'{name}={value}; Domain=.bilibili.com; Path=/; Secure; HttpOnly')
                    response.cookies.set(name, value, domain='.bilibili.com', secure=True)
        elif path.endswith('/confirm/refresh'):
            if self.on_confirm:
                self.on_confirm()
            if self.confirm_error:
                raise requests.Timeout('private-token-error')
            body = {'code': 0}
        elif path.endswith('/view'):
            body = {'code': 0, 'data': {'title': 'fixture'}}
        else:
            raise AssertionError('意外网络请求')
        response._content = json.dumps(body).encode()
        response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=headers))
        return response

    def close(self):
        pass


class RefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / 'cookie.txt'
        with BiliSession(self.path) as session:
            session.import_login('SESSDATA=old-session; bili_jct=old-csrf; buvid3=device', 'old-token', sanitize_cookie)
        self.source = self.make_source()
        self.adapter = PassportAdapter()
        self.source.session.mount('https://', self.adapter)
        self.addCleanup(self.source.session.close)

    def make_source(self, **kwargs):
        return BiliVideoSource(cookie_file=str(self.path), download_cache_dir=self.temporary.name, **kwargs)

    def restored(self):
        source = self.make_source()
        self.addCleanup(source.session.close)
        return source.session

    def test_production_video_entry_rotates_before_acquisition(self):
        def assert_saved_before_confirm():
            saved = self.restored()
            self.assertEqual(cookie_value(saved, 'SESSDATA'), 'new-session')
            self.assertEqual(saved._metadata['refresh_token'], 'new-token')
            self.assertEqual(saved._metadata['pending_confirm'], 'old-token')
        self.adapter.on_confirm = assert_saved_before_confirm
        self.assertEqual(self.source.get_video_info('BV1Mbp76oESC')['title'], 'fixture')
        self.assertEqual(len(self.adapter.calls), 5)
        refresh = self.adapter.calls[2]
        confirm = self.adapter.calls[3]
        self.assertEqual(parse_qs(refresh.body)['refresh_token'], ['old-token'])
        self.assertEqual(parse_qs(refresh.body)['csrf'], ['old-csrf'])
        self.assertEqual(parse_qs(confirm.body)['refresh_token'], ['old-token'])
        self.assertEqual(parse_qs(confirm.body)['csrf'], ['new-csrf'])
        self.assertIn('SESSDATA=new-session', self.adapter.calls[4].headers['Cookie'])
        saved = self.restored()
        self.assertNotIn('pending_confirm', saved._metadata)
        self.assertNotIn('old-token', self.path.read_text(encoding='utf-8'))
        # 新会话仍沿用持久检查时间，避免每次构造都刷新。
        saved.mount('https://', self.adapter)
        saved.ensure_login()
        self.assertEqual(len(self.adapter.calls), 5)

    def test_no_refresh_only_checks_once(self):
        self.adapter.refresh = False
        self.source._ensure_device_cookies()
        self.source._ensure_device_cookies()
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertEqual(self.restored()._metadata['refresh_token'], 'old-token')

    def test_disabled_or_missing_token_does_not_request(self):
        self.source.auto_refresh = False
        self.source._ensure_device_cookies()
        self.assertEqual(self.adapter.calls, [])
        self.path.write_text('SESSDATA=manual; buvid3=device', encoding='utf-8')
        self.source.auto_refresh = True
        self.source._ensure_device_cookies()
        self.assertEqual(self.adapter.calls, [])
        self.assertEqual(cookie_value(self.source.session, 'SESSDATA'), 'manual')

    def test_refresh_failure_keeps_credentials_and_throttles_after_restart(self):
        self.adapter.rotate_error = True
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(BiliSourceError) as error:
            self.source._ensure_device_cookies()
        self.assertEqual(error.exception.code, 'refresh')
        self.assertNotIn('private-server-secret', str(error.exception) + output.getvalue())
        saved = self.restored()
        self.assertEqual(cookie_value(saved, 'SESSDATA'), 'old-session')
        self.assertEqual(saved._metadata['refresh_token'], 'old-token')
        saved.mount('https://', self.adapter)
        before = len(self.adapter.calls)
        with self.assertRaises(BiliRefreshError):
            saved.ensure_login()
        self.assertEqual(len(self.adapter.calls), before)

    def test_confirm_timeout_recovers_using_new_cookie_and_old_token(self):
        self.adapter.confirm_error = True
        with self.assertRaises(BiliSourceError):
            self.source._ensure_device_cookies()
        saved = self.restored()
        self.assertEqual(saved._metadata['refresh_token'], 'new-token')
        self.assertEqual(saved._metadata['pending_confirm'], 'old-token')
        self.adapter.confirm_error = False
        saved.mount('https://', self.adapter)
        before = len(self.adapter.calls)
        with patch('integration.bili_cookies.time.time', return_value=saved._metadata['last_attempt'] + 3601):
            saved.ensure_login()
        self.assertEqual(len(self.adapter.calls), before + 1)
        request = self.adapter.calls[-1]
        self.assertTrue(request.url.endswith('/confirm/refresh'))
        self.assertIn('SESSDATA=new-session', request.headers['Cookie'])
        self.assertEqual(parse_qs(request.body)['refresh_token'], ['old-token'])
        self.assertNotIn('pending_confirm', self.restored()._metadata)

    def test_malformed_html_or_missing_set_cookie_cannot_confirm(self):
        for malformed in ('html', 'cookies'):
            with self.subTest(malformed=malformed):
                with BiliSession(self.path) as session:
                    session.import_login('SESSDATA=old-session; bili_jct=old-csrf; buvid3=device', 'old-token', sanitize_cookie)
                self.adapter.html = '<div>missing</div>' if malformed == 'html' else '<div id="1-name">csrf</div>'
                self.adapter.missing_cookies = malformed == 'cookies'
                with self.assertRaises(BiliSourceError):
                    self.source._ensure_device_cookies()
                self.assertFalse(any(call.url.endswith('/confirm/refresh') for call in self.adapter.calls))
                self.assertEqual(self.restored()._metadata['refresh_token'], 'old-token')

    def test_login_failure_and_file_lock_are_explicit(self):
        self.adapter.info_code = -101
        with self.assertRaises(BiliSourceError) as error:
            self.source._ensure_device_cookies()
        self.assertEqual(error.exception.code, -101)
        with worker_lock(str(self.path) + '.lock'):
            with self.assertRaises(BiliSourceError):
                self.source._ensure_device_cookies()

    def test_failed_new_credentials_save_never_confirms(self):
        original_save = self.source.session._save
        def fail_new_credentials():
            if self.source.session._metadata.get('pending_confirm'):
                raise OSError('private-write-error')
            original_save()
        with patch.object(self.source.session, '_save', side_effect=fail_new_credentials):
            with self.assertRaises(BiliSourceError):
                self.source._ensure_device_cookies()
        self.assertFalse(any(call.url.endswith('/confirm/refresh') for call in self.adapter.calls))
        self.assertEqual(self.restored()._metadata['refresh_token'], 'old-token')

    def test_correspond_path_is_randomized_rsa_oaep(self):
        first = correspond_path(1700000000000)
        self.assertRegex(first, r'^[0-9a-f]{256}$')
        self.assertNotEqual(first, correspond_path(1700000000000))

    def test_service_import_and_config_pass_through(self):
        path = Path(self.temporary.name) / 'config.toml'
        path.write_text((ROOT / 'config.example.toml').read_text(encoding='utf-8'), encoding='utf-8')
        settings = Settings(config_file=path, data_dir=Path(self.temporary.name) / 'data', cookie_file=str(self.path))
        service = DailyService(settings)
        service.configure_bili_login('SESSDATA=manual; bili_jct=csrf', 'manual-token')
        source = service.bili()
        self.addCleanup(source.session.close)
        self.assertTrue(source.auto_refresh)
        self.assertEqual(source.refresh_check_seconds, settings.bili_refresh_check_seconds)
        self.assertEqual(source.session._metadata['refresh_token'], 'manual-token')
        self.assertEqual(service.dashboard()['tasks'], [])
        self.assertNotIn('manual-token', repr(settings))
