"""HTTP入口大小限制与表单校验；只使用临时配置和离线请求。"""
import asyncio
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from config import ROOT, Settings
from interaction.request_limits import RequestBodyLimitMiddleware
from interaction.web import create_app


class BodyMiddlewareTests(unittest.TestCase):
    def run_request(self, chunks, headers=(), limit=16):
        messages = []
        calls = []
        received = []

        async def app(scope, receive, send):
            calls.append(True)
            received.append((await receive())['body'])
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': received[-1]})

        async def run():
            stream = iter(chunks)

            async def receive():
                return next(stream)

            async def send(message):
                messages.append(message)

            await RequestBodyLimitMiddleware(app, limit)({'type': 'http', 'headers': list(headers)}, receive, send)

        asyncio.run(run())
        return messages, calls, received

    def test_exact_limit_replayed_intact_before_downstream(self):
        frames = [{'type':'http.request', 'body':b'a'*8, 'more_body':True},
                  {'type':'http.request', 'body':b'b'*8, 'more_body':False}]
        messages, calls, received = self.run_request(frames)
        self.assertEqual(messages[0]['status'], 200)
        self.assertEqual(calls, [True])
        self.assertEqual(received, [b'a'*8+b'b'*8])

    def test_missing_or_understated_length_cannot_bypass_actual_byte_limit(self):
        for headers in ([], [(b'content-length',b'1')], [(b'transfer-encoding',b'chunked')]):
            frames = [{'type':'http.request', 'body':b'a'*8, 'more_body':True},
                      {'type':'http.request', 'body':b'b'*9, 'more_body':True}]
            messages, calls, _ = self.run_request(frames, headers)
            self.assertEqual(messages[0]['status'], 413)
            self.assertFalse(calls)

    def test_declared_oversize_rejected_without_reading_body(self):
        messages, calls, _ = self.run_request([], [(b'content-length',b'17')])
        self.assertEqual(messages[0]['status'], 413)
        self.assertFalse(calls)

    def test_malformed_or_duplicate_length_rejected_without_reading(self):
        for values in ([b'-1'], [b'not-a-length'], [b'9'*100], [b'1',b'1']):
            messages, calls, _ = self.run_request([], [(b'content-length',v) for v in values])
            self.assertEqual(messages[0]['status'], 400)
            self.assertFalse(calls)

    def test_disconnect_does_not_run_downstream(self):
        messages, calls, _ = self.run_request([{'type':'http.disconnect'}])
        self.assertFalse(messages)
        self.assertFalse(calls)


class RequestLimitTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name)/'config.toml'
        self.template = (ROOT/'config.example.toml').read_text(encoding='utf-8')
        self.path.write_text(self.template, encoding='utf-8')
        self.settings = Settings(config_file=self.path, admin_hash='offline-hash', session_secret='offline-limit-secret-at-least-32-characters', secure_cookie=False)
        self.app = create_app(self.settings, run_background=False)
        self.service = self.app.state.service
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        session = self.service.new_session(True)
        self.client.cookies.set('daily_session', session)
        self.token = self.service.session(session)['csrf']

    def test_defaults_old_config_fallback_and_configured_limits(self):
        legacy = re.sub(r'\[web_limits\]\n.*?(?=\[site\])', '', self.template, flags=re.S)
        self.path.write_text(legacy, encoding='utf-8')
        self.assertEqual(Settings(config_file=self.path).web_limits, self.settings.web_limits)
        self.path.write_text(self.template.replace('request_body_bytes = 65536', 'request_body_bytes = 1024').replace('source_name_chars = 200','source_name_chars = 12'), encoding='utf-8')
        settings = Settings(config_file=self.path)
        self.assertEqual(settings.web_limits['request_body_bytes'], 1024)
        with TestClient(create_app(settings, run_background=False)) as client:
            self.assertEqual(client.post('/admin/login', content=b'x'*1025).status_code, 413)
            self.assertEqual(client.post('/admin/sources', data={'kind':'bili','target':'123','name':'x'*13,'token':'x'}).status_code, 422)

    def test_bad_limit_configuration_fails_startup(self):
        for assignment in ('request_body_bytes = true', 'request_body_bytes = 0', 'request_body_bytes = 1048577', 'source_name_chars = -1', 'password_chars = 1025', 'target_chars = "4096"'):
            key = assignment.split(' = ')[0]
            self.path.write_text(re.sub(rf'^{key} = .*$', assignment, self.template, flags=re.M), encoding='utf-8')
            with self.subTest(assignment=assignment), self.assertRaises(ValueError):
                Settings(config_file=self.path)

    def test_oversize_form_multipart_and_chunked_rejected_before_parser(self):
        maximum = self.settings.web_limits['request_body_bytes']
        with patch('starlette.requests.Request._get_form', side_effect=AssertionError('must not parse oversized bodies')):
            responses = [
                self.client.post('/admin/login', data={'password':'x'*maximum,'token':self.token}),
                self.client.post('/admin/video', files={'file':('payload.txt',b'x'*maximum)}),
                self.client.post('/admin/login', content=iter([b'x'*(maximum//2),b'x'*(maximum//2+1)]), headers={'Content-Type':'application/x-www-form-urlencoded'}),
            ]
        for response in responses:
            self.assertEqual(response.status_code, 413)
            self.assertEqual(response.headers['cache-control'], 'no-store')
            self.assertIn('nosniff', response.headers['x-content-type-options'])

    def test_all_form_entrypoints_reject_long_fields_before_business_calls(self):
        source = self.service.add_source('bili','123','original')
        base = {'kind':'bili','target':'123','name':'normal','token':self.token}
        cases = [
            ('/admin/login', {'password':'private-password-'+'x'*1024,'token':self.token}, 'login'),
            ('/admin/sources', base|{'target':'x'*4097}, 'add_source'),
            ('/admin/sources', base|{'name':'名'*201}, 'add_source'),
            ('/admin/sources', base|{'kind':'x'*17}, 'add_source'),
            (f'/admin/sources/{source}/edit', base|{'name':'名'*201}, 'edit_source'),
            ('/admin/sources/order', {'order':'1'*16385,'token':self.token}, 'reorder_sources'),
            ('/admin/video', {'target':'x'*4097,'token':self.token}, 'submit_video'),
            ('/admin/rebuild', {'day':'2026-10-190','token':self.token}, 'request_rebuild'),
            ('/admin/logout', {'token':'private-token-'+'x'*128}, 'logout'),
            (f'/admin/sources/{source}/delete', {'token':'x'*129}, 'source_action'),
            ('/admin/tasks/1/retry', {'token':'x'*129}, 'retry'),
        ]
        for url, data, method in cases:
            with self.subTest(url=url,method=method), patch.object(self.service, method) as call:
                response = self.client.post(url, data=data, follow_redirects=False)
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json(), {'detail':'请求字段缺失、格式错误或超过长度限制'})
                self.assertNotIn('private-password-', response.text)
                self.assertNotIn('private-token-', response.text)
                call.assert_not_called()
        self.assertEqual(self.service.get_source(source)['name'], 'original')
        self.assertEqual(len(self.service.store.sources()), 1)

    def test_exact_field_boundaries_and_unicode_accepted(self):
        with patch.object(self.service, 'add_source') as add:
            response = self.client.post('/admin/sources', data={'kind':'rss','target':'https://example.com/'+'a'*(4096-20),'name':'名'*200,'token':self.token}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            add.assert_called_once()
            self.assertEqual(len(add.call_args.args[1]), 4096)
            self.assertEqual(add.call_args.args[2], '名'*200)
        with patch.object(self.service, 'login', return_value=True) as login:
            response = self.client.post('/admin/login', data={'password':'p'*1024,'token':self.token}, follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            login.assert_called_once()

    def test_missing_field_error_does_not_echo_password(self):
        response = self.client.post('/admin/login', data={'password':'private-password-fixture'})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('private-password-fixture', response.text)
