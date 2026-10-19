import re
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient
from config import Settings
from integration.auth import hash_password
from interaction.web import create_app

class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.settings=Settings(data_dir=Path(self.directory.name),session_secret='session-test-secret-at-least-32-characters',admin_hash=hash_password('abcdefghij'),secure_cookie=False)
        self.app=create_app(self.settings, run_background=False)
        self.client=TestClient(self.app)

    def tearDown(self):
        self.client.close();self.directory.cleanup()

    def test_daily_navigation_and_login_page_preserve_session(self):
        page=self.client.get('/admin/login')
        token=re.search('name="token" value="([^"]+)"',page.text)[1]
        dashboard=self.client.post('/admin/login',data={'password':'abcdefghij','token':token})
        self.assertEqual(dashboard.status_code,200)
        before=self.app.state.service.session(self.client.cookies.get('daily_session'))
        home=self.client.get('/')
        self.assertIn('管理后台 · 已登录',home.text)
        self.assertIn('no-store',home.headers['cache-control'])
        login=self.client.get('/admin/login',follow_redirects=False)
        self.assertEqual(login.status_code,303)
        self.assertEqual(login.headers['location'],'/admin')
        after=self.app.state.service.session(self.client.cookies.get('daily_session'))
        self.assertEqual(before,after)
        self.assertEqual(self.client.get('/admin').status_code,200)
        self.client.post('/admin/logout',data={'token':after['csrf']})
        self.assertIn('管理登录',self.client.get('/').text)
        self.assertEqual(self.client.get('/admin').status_code,401)

    def test_legacy_admin_path_cookie_migrates(self):
        token=self.app.state.service.new_session(True)
        self.client.cookies.set('daily_session',token,domain='testserver.local',path='/admin')
        self.assertNotIn('已登录',self.client.get('/').text)
        result=self.client.get('/admin/login')
        self.assertEqual(result.status_code,200)
        self.assertIn('已登录',self.client.get('/').text)
        cookies=[c for c in self.client.cookies.jar if c.name=='daily_session']
        self.assertEqual([c.path for c in cookies],['/'])

    def test_invalid_signature_cannot_enter_admin(self):
        self.client.cookies.set('daily_session','invalid-signature',domain='testserver.local',path='/')
        self.assertNotIn('已登录',self.client.get('/').text)
        self.assertEqual(self.client.get('/admin').status_code,401)
