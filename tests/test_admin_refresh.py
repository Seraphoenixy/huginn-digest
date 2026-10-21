import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from config import ROOT, Settings
from interaction.web import create_app


class AdminRefreshTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config=Path(self.directory.name)/'config.toml'
        self.template=(ROOT/'config.example.toml').read_text(encoding='utf-8')
        self.config.write_text(self.template,encoding='utf-8')
        self.settings=Settings(config_file=self.config,session_secret='offline-refresh-secret-at-least-32-characters',admin_hash='offline-hash',secure_cookie=False)
        self.app=create_app(self.settings,run_background=False)
        self.service=self.app.state.service
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)

    def test_refresh_requires_admin_and_stops_after_revocation(self):
        self.assertEqual(self.client.get('/admin').status_code,401)
        session=self.service.new_session(True)
        self.client.cookies.set('daily_session',session)
        page=self.client.get('/admin')
        self.assertEqual(page.status_code,200)
        self.assertIn('no-store',page.headers['cache-control'])
        self.assertIn('data-refresh-seconds="5"',page.text)
        self.assertIn('/static/admin-refresh.js',page.text)
        self.service.logout(session)
        self.assertEqual(self.client.get('/admin').status_code,401)

    def test_new_status_rendered_without_processing_or_exposing_raw_html(self):
        self.client.cookies.set('daily_session',self.service.new_session(True))
        source=self.service.add_source('bili','123','<script>private-source</script>')
        self.service.enqueue('rss',{'url':'https://example.com/article','title':'示例任务','published':time.time()})
        first=self.client.get('/admin')
        self.assertIn('等待处理',first.text)
        self.service.store.execute("UPDATE tasks SET status='done',stage='finish'")
        self.service.store.execute('UPDATE sources SET error=? WHERE id=?',('来源暂时不可用',source))
        self.service.store.set_meta('heartbeat',time.time())
        with patch.object(self.service,'tick',side_effect=AssertionError('GET must not process tasks')):
            updated=self.client.get('/admin')
        self.assertIn('处理完成',updated.text)
        self.assertIn('来源暂时不可用',updated.text)
        self.assertNotIn('<script>private-source</script>',updated.text)
        self.assertIn('&lt;script&gt;private-source&lt;/script&gt;',updated.text)
        for region in ('heartbeat','source-count','sources','task-count','tasks','rebuilds'):
            self.assertIn(f'data-live-region="{region}"',updated.text)
        self.assertIn('placeholder="MID、空间链接或RSS地址"',updated.text)

    def test_refresh_setting_validation_old_config_and_script(self):
        legacy=re.sub(r'^admin_refresh_seconds = .*\n','',self.template,flags=re.M)
        self.config.write_text(legacy,encoding='utf-8')
        self.assertEqual(Settings(config_file=self.config).admin_refresh_seconds,5)
        for value in ('0','301','true','"5"'):
            self.config.write_text(self.template.replace('admin_refresh_seconds = 5',f'admin_refresh_seconds = {value}'),encoding='utf-8')
            with self.subTest(value=value),self.assertRaises(ValueError):Settings(config_file=self.config)
        self.config.write_text(self.template.replace('admin_refresh_seconds = 5','admin_refresh_seconds = 1'),encoding='utf-8')
        settings=Settings(config_file=self.config,session_secret=self.settings.session_secret)
        app=create_app(settings,run_background=False)
        with TestClient(app) as client:
            client.cookies.set('daily_session',app.state.service.new_session(True))
            self.assertIn('data-refresh-seconds="1"',client.get('/admin').text)
        response=self.client.get('/static/admin-refresh.js')
        self.assertEqual(response.status_code,200)
        self.assertIn('javascript',response.headers['content-type'])
