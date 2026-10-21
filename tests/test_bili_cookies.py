import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from integration.sources.bili import BiliVideoSource, BiliSourceError


class CookieTests(unittest.TestCase):
    def test_unknown_login_metadata_is_ignored(self):
        from integration.bili_cookies import BiliSession, META
        from integration.sources.bili import sanitize_cookie
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cookie.txt'
            with BiliSession(path) as session:
                session.import_login('SESSDATA=fixture; bili_jct=fixture', 'fixture-token', sanitize_cookie)
            # 非本程序写入的元数据行不得被当作刷新口令读取。
            path.write_text(path.read_text(encoding='utf-8').replace(META, '# other-tool-login: '),
                            encoding='utf-8')
            source = BiliVideoSource(cookie_file=str(path), download_cache_dir=directory)
            self.assertEqual(source.session._metadata, {})
            source.session.persist()
            self.assertNotIn('# other-tool-login: ', path.read_text(encoding='utf-8'))
            source.session.close()
    def test_real_requests_extracts_set_cookie_before_saving(self):
        from email.message import Message
        from types import SimpleNamespace
        from requests.adapters import BaseAdapter

        class Adapter(BaseAdapter):
            def send(self, request, **kwargs):
                response = requests.Response()
                response.status_code = 200
                response._content = b'{}'
                response.request = request
                response.url = request.url
                headers = Message()
                headers.add_header('Set-Cookie', 'SESSDATA=rotated; Domain=.bilibili.com; Path=/; Secure; HttpOnly')
                response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=headers))
                return response

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cookies.txt'
            path.write_text('SESSDATA=old', encoding='utf-8')
            source = BiliVideoSource(cookie_file=str(path), download_cache_dir=directory)
            source.session.mount('https://', Adapter())
            source.session.get('https://api.bilibili.com/test')
            source.session.close()
            restored = BiliVideoSource(cookie_file=str(path), download_cache_dir=directory)
            self.assertEqual(restored.session.cookies.get('SESSDATA'), 'rotated')
            restored.session.close()

    def test_rotation_persistence_and_scope(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cookies.txt'
            path.write_text('SESSDATA=old; buvid3=device', encoding='utf-8')
            source = BiliVideoSource(cookie_file=str(path), download_cache_dir=directory)
            self.assertNotIn('Cookie', source.session.headers)
            expires = int(time.time()) + 3600

            def server_send(session, request, **kwargs):
                session.cookies.set('SESSDATA', 'new', domain='.bilibili.com', secure=True, expires=expires)
                session.cookies.set('foreign', 'private', domain='.example.com')
                session.cookies.set('expired', 'old', domain='.bilibili.com', expires=1)
                return Mock()

            with patch('requests.Session.send', server_send):
                source.session.get('https://api.bilibili.com/test')
            source.session.close()
            restored = BiliVideoSource(cookie_file=str(path), download_cache_dir=directory)
            cookies = {c.name: c for c in restored.session.cookies}
            self.assertEqual(cookies['SESSDATA'].value, 'new')
            self.assertEqual(cookies['SESSDATA'].expires, expires)
            self.assertNotIn('foreign', cookies)
            self.assertNotIn('expired', cookies)
            prepared = restored.session.prepare_request(requests.Request('GET', 'https://api.bilibili.com/test'))
            self.assertIn('SESSDATA=new', prepared.headers['Cookie'])
            for url in ('http://api.bilibili.com/', 'https://example.com/'):
                request = restored.session.prepare_request(requests.Request('GET', url))
                self.assertNotIn('Cookie', request.headers)
            restored.session.close()

    def test_external_edit_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cookies.txt'
            path.write_text('SESSDATA=old', encoding='utf-8')
            source = BiliVideoSource(cookie_file=str(path), download_cache_dir=directory)
            path.write_text('SESSDATA=manual', encoding='utf-8')
            source.session.persist()
            self.assertEqual(path.read_text(encoding='utf-8'), 'SESSDATA=manual')
            source.session.close()

    def test_failed_save_keeps_original_and_redacts_error(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cookies.txt'
            path.write_text('SESSDATA=old', encoding='utf-8')
            source = BiliVideoSource(cookie_file=str(path), download_cache_dir=directory)
            output = io.StringIO()
            with patch('integration.bili_cookies.os.replace', side_effect=OSError('private-secret')):
                with contextlib.redirect_stdout(output):
                    source.session.persist()
            self.assertNotIn('private-secret', output.getvalue())
            self.assertEqual(path.read_text(encoding='utf-8'), 'SESSDATA=old')
            self.assertEqual(list(Path(directory).glob('.cookie-*')), [])
            source.session.close()

    def test_nav_expired_login_is_explicit(self):
        source = BiliVideoSource(download_cache_dir='unused')
        source._device_cookies_ready = True
        source.session.get = Mock(return_value=Mock(json=lambda: {'code': -101, 'message': 'private'}))
        with self.assertRaises(BiliSourceError) as raised:
            source._get_wbi_mixin_key()
        self.assertEqual(raised.exception.code, -101)
        self.assertNotIn('private', str(raised.exception))
        source.session.close()
