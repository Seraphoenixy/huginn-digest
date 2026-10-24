import json
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

    def login(self):
        self.client.cookies.set('daily_session',self.service.new_session(True))

    def test_refresh_requires_admin_and_stops_after_revocation(self):
        self.assertEqual(self.client.get('/admin').status_code,401)
        self.assertEqual(self.client.get('/admin/status').status_code,401)
        session=self.service.new_session(True)
        self.client.cookies.set('daily_session',session)
        page=self.client.get('/admin')
        self.assertEqual(page.status_code,200)
        self.assertIn('no-store',page.headers['cache-control'])
        self.assertIn('data-refresh-seconds="5"',page.text)
        self.assertIn('data-live-endpoint="/admin/status"',page.text)
        self.assertIn('/static/admin-refresh.js',page.text)
        self.service.logout(session)
        self.assertEqual(self.client.get('/admin').status_code,401)

    def test_new_status_rendered_without_processing_or_exposing_raw_html(self):
        self.login()
        source=self.service.add_source('bili','123','<script>private-source</script>')
        self.service.enqueue('rss',{'url':'https://example.com/article','title':'示例任务','published':time.time()})
        first=self.client.get('/admin')
        self.assertIn('等待处理',first.text)
        self.service.store.execute("UPDATE tasks SET status='done',stage='finish'")
        self.service.store.execute('UPDATE sources SET error=? WHERE id=?',('来源暂时不可用',source))
        self.service.store.set_meta('heartbeat',time.time())
        with patch.object(self.service,'tick',side_effect=AssertionError('GET must not process tasks')):
            updated=self.client.get('/admin')
        self.assertIn('已完成',updated.text)
        self.assertIn('来源暂时不可用',updated.text)
        self.assertNotIn('<script>private-source</script>',updated.text)
        self.assertIn('&lt;script&gt;private-source&lt;/script&gt;',updated.text)
        for region in ('heartbeat','source-count','sources','task-count','task-panel'):
            self.assertIn(f'data-live-region="{region}"',updated.text)
        self.assertIn('placeholder="MID、空间链接或RSS地址"',updated.text)

    def test_single_task_panel_replaces_duplicate_lists(self):
        """最近任务与重建曾重复列出同一批内容；现在由视图切换，同页只出现一个列表。"""
        self.login()
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'唯一任务标题','published':time.time()})
        page=self.client.get('/admin').text
        self.assertEqual(page.count('唯一任务标题'),1)
        self.assertNotIn('重新汇集日报</h2>',page)
        self.assertIn('view=rebuilds',page)

    def test_task_views_and_day_filter(self):
        self.login()
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'任务甲','published':time.time()})
        self.service.enqueue('rss',{'url':'https://example.com/b','title':'任务乙','published':time.time()-86400})
        self.service.store.execute("UPDATE tasks SET status='failed',stage='summarize',error='summarize: CloudError' WHERE title='任务甲'")
        failed=self.client.get('/admin?view=failed')
        self.assertEqual(failed.status_code,200)
        self.assertIn('任务甲',failed.text)
        self.assertNotIn('任务乙',failed.text)
        self.assertIn('生成摘要 · 云端模型调用失败',failed.text)
        self.assertEqual(self.client.get('/admin?view=unknown').status_code,200)
        self.assertEqual(self.client.get('/admin?day=2026-13-01').status_code,400)

    def test_activity_status_is_realtime_and_read_only(self):
        self.login()
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'实时任务','published':time.time()})
        self.service.runtime.begin('task','处理任务 #1',detail='下载分P 1/3',task_id=1,title='实时任务',stage='acquire',current=1,total=3)
        self.service.store.set_meta('heartbeat',time.time())
        with patch.object(self.service,'tick',side_effect=AssertionError('status must not process tasks')):
            response=self.client.get('/admin/status')
        self.assertEqual(response.status_code,200)
        self.assertIn('no-store',response.headers['cache-control'])
        payload=response.json()
        self.assertEqual(payload['kind'],'task')
        self.assertEqual(payload['stage_label'],'获取正文')
        self.assertEqual(payload['progress'],33)
        self.assertEqual(payload['task_counts']['queued'],1)
        self.assertEqual(payload['llm']['configured'],False)

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
        script=response.text
        self.assertIn("'/admin/status'",script)
        self.assertIn('listInterval',script)


class ModelReloadTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config=Path(self.directory.name)/'config.toml'
        self.template=(ROOT/'config.example.toml').read_text(encoding='utf-8')
        self.config.write_text(self.template,encoding='utf-8')
        self.settings=Settings(config_file=self.config,session_secret='offline-reload-secret-at-least-32-characters',admin_hash='offline-hash',secure_cookie=False)
        self.app=create_app(self.settings,run_background=False)
        self.service=self.app.state.service
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)
        self.client.cookies.set('daily_session',self.service.new_session(True))

    def rewrite(self,old,new):
        text=self.config.read_text(encoding='utf-8')
        self.assertIn(old,text)
        self.config.write_text(text.replace(old,new),encoding='utf-8')

    def test_llm_settings_reload_without_restart(self):
        before=self.service.settings.llm_state()
        self.assertEqual(before['model'],'deepseek-v4.1-flash')
        self.rewrite('model = "deepseek-v4.1-flash"','model = "fixture-model-next"')
        self.assertTrue(self.service.settings.refresh_llm())
        after=self.service.settings.llm_state()
        self.assertEqual(after['model'],'fixture-model-next')
        self.assertEqual(after['revision'],before['revision']+1)
        self.assertEqual(self.service.settings.get('llm','model'),'fixture-model-next')

    def test_invalid_change_keeps_previous_config_and_reports_reason(self):
        before=self.service.settings.llm_state()
        self.rewrite('base_url = "https://ark.cn-beijing.volces.com/api/plan/v3"','base_url = "http://insecure.example.com/v1"')
        self.assertFalse(self.service.settings.refresh_llm())
        after=self.service.settings.llm_state()
        self.assertEqual(after['model'],before['model'])
        self.assertIn('base_url',after['error'])
        self.assertEqual(after['revision'],before['revision'])

    def test_unparsable_file_keeps_running_config(self):
        before=self.service.settings.get('llm','model')
        self.config.write_text('this is not toml [',encoding='utf-8')
        self.assertFalse(self.service.settings.refresh_llm())
        self.assertEqual(self.service.settings.get('llm','model'),before)
        self.assertTrue(self.service.settings.llm_state()['error'])

    def test_admin_reload_endpoint_requires_csrf_and_reports_failure(self):
        self.assertEqual(self.client.post('/admin/config/reload',data={'token':'wrong'}).status_code,403)
        token=self.service.session(self.client.cookies.get('daily_session'))['csrf']
        self.assertEqual(self.client.post('/admin/config/reload',data={'token':token},follow_redirects=False).status_code,303)
        self.config.write_text('broken [',encoding='utf-8')
        response=self.client.post('/admin/config/reload',data={'token':token},follow_redirects=False)
        self.assertEqual(response.status_code,409)
        marker=self.service.settings.llm_reloaded_at
        self.assertGreater(marker,0)

    def test_reload_deferred_while_task_running(self):
        self.rewrite('model = "deepseek-v4.1-flash"','model = "fixture-model-next"')
        self.service.runtime.begin('task','处理任务 #9',detail='摘要中',task_id=9)
        self.assertFalse(self.service.refresh_config())
        self.assertEqual(self.service.settings.get('llm','model'),'deepseek-v4.1-flash')
        self.service.runtime.finish('空闲','等待下一轮调度')
        self.assertTrue(self.service.refresh_config())
        self.assertEqual(self.service.settings.get('llm','model'),'fixture-model-next')

    def test_config_page_never_exposes_key_but_shows_endpoint(self):
        self.rewrite('[llm]\napi_key = ""','[llm]\napi_key = "private-key-fixture"')
        self.service.settings.refresh_llm(force=True)
        page=self.client.get('/admin').text
        # 密钥任何形态都不出现；接口地址与模型名按要求完整回显给已登录管理员。
        self.assertNotIn('private-key-fixture',page)
        self.assertIn('https://ark.cn-beijing.volces.com/api/plan/v3',page)


if __name__=='__main__':unittest.main()
