import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from config import ROOT, Settings
from integration.auth import hash_password
from interaction.web import create_app


class LoginFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        config=Path(self.directory.name)/'config.toml'
        config.write_text((ROOT/'config.example.toml').read_text(encoding='utf-8'),encoding='utf-8')
        self.settings=Settings(config_file=config,admin_hash=hash_password('offline-password'),session_secret='offline-feedback-secret-at-least-32-characters',secure_cookie=False)
        self.app=create_app(self.settings,run_background=False)
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)

    def csrf(self,response):
        return re.search('name="token" value="([^"]+)"',response.text)[1]

    def test_wrong_password_renders_dialog_and_can_retry(self):
        token=self.csrf(self.client.get('/admin/login'))
        response=self.client.post('/admin/login',data={'password':'wrong-private-password','token':token})
        self.assertEqual(response.status_code,401)
        self.assertIn('text/html',response.headers['content-type'])
        self.assertIn('id="login-error-dialog"',response.text)
        self.assertIn('登录未成功',response.text)
        self.assertIn('src="/static/login.js"',response.text)
        self.assertIn('role="alert"',response.text)
        self.assertIn('no-store',response.headers['cache-control'])
        self.assertNotIn('wrong-private-password',response.text)
        self.assertNotIn('unsafe-inline',response.headers['content-security-policy'].split('script-src')[1].split(';')[0])
        self.assertEqual(self.client.get('/admin').status_code,401)
        success=self.client.post('/admin/login',data={'password':'offline-password','token':self.csrf(response)},follow_redirects=False)
        self.assertEqual(success.status_code,303)
        self.assertEqual(success.headers['location'],'/admin')

    def test_lockout_keeps_login_page_and_does_not_authenticate(self):
        page=self.client.get('/admin/login')
        for _ in range(5):
            page=self.client.post('/admin/login',data={'password':'wrong-password','token':self.csrf(page)})
        response=self.client.post('/admin/login',data={'password':'offline-password','token':self.csrf(page)})
        self.assertEqual(response.status_code,401)
        self.assertIn('id="login-error-dialog"',response.text)
        self.assertIn('稍后重试',response.text)
        self.assertEqual(self.client.get('/admin').status_code,401)

    def test_invalid_csrf_renders_retryable_form_without_checking_password(self):
        with patch.object(self.app.state.service,'login') as login:
            response=self.client.post('/admin/login',data={'password':'offline-password','token':'expired'})
            login.assert_not_called()
        self.assertEqual(response.status_code,403)
        self.assertIn('登录表单已过期',response.text)
        success=self.client.post('/admin/login',data={'password':'offline-password','token':self.csrf(response)},follow_redirects=False)
        self.assertEqual(success.status_code,303)

    def test_script_is_served_and_normal_login_has_no_error_dialog(self):
        page=self.client.get('/admin/login')
        self.assertNotIn('id="login-error-dialog"',page.text)
        script=self.client.get('/static/login.js')
        self.assertEqual(script.status_code,200)
        self.assertIn('javascript',script.headers['content-type'])
        self.assertIn('showModal()',script.text)

    def test_unconfigured_session_does_not_crash_error_rendering(self):
        self.app.state.service.sessions.ready=False
        response=self.client.post('/admin/login',data={'password':'offline-password','token':'expired'})
        self.assertEqual(response.status_code,503)
        self.assertIn('管理入口尚未配置',response.text)
