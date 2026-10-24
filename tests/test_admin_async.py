"""管理页就地更新：写操作与视图切换不整页刷新，操作后保留当前视图。"""
import json
import re
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from config import Settings
from integration.auth import hash_password
from integration.files import write_text
from interaction.web import create_app
from orchestration.service import DailyService, day_of


class AdminAsyncTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='admin-async-secret-more-than-32-chars',
                               admin_hash=hash_password('abcdefghij'),secure_cookie=False)
        self.service=DailyService(self.settings)
        self.pipeline=self.service._ensure_pipeline()
        self.now=time.time()
        self.app=create_app(self.settings,run_background=False)
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)
        self.day,self.task=self.seed()

    def seed(self):
        day=day_of(self.now)
        url='https://example.com/async'
        self.service.enqueue('rss',{'url':url,'title':'就地更新','published':time.time()})
        task=self.service.store.query('SELECT * FROM tasks WHERE url=?',(url,))[0]
        self.service.store.execute('UPDATE tasks SET day=? WHERE id=?',(day,task['id']))
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'raw.txt','正文内容')
        write_text(folder/'summary.json',json.dumps({'topic':'AI 与大模型','bullets':['要点']},ensure_ascii=False))
        write_text(folder/'summary_manifest.json',
                   json.dumps({'fingerprint':self.pipeline.summary_fingerprint(folder/'raw.txt')}))
        self.service.store.update(task['id'],status='done',
                                  summary_path=str(folder/'summary.json'),raw_path=str(folder/'raw.txt'))
        self.service.publish(day)
        self.client.cookies.set('daily_session',self.service.new_session(True))
        return day,task

    def token(self):
        return re.search('name="token" value="([^"]+)"',self.client.get('/admin').text)[1]

    def test_actions_are_marked_for_in_place_update(self):
        page=self.client.get('/admin').text
        self.assertIn('id="dashboard-action-status"',page)
        self.assertIn('data-async>',page)
        self.assertIn('data-async-nav',page)
        # 视图切换与写操作都要带上当前视图，服务端据此回到原视图。
        self.assertIn('name="view"',page)
        # 清理与模型卡片整体纳入片段刷新范围。
        self.assertIn('data-live-region="llm-card"',page)
        self.assertIn('data-live-region="storage-card"',page)

    def test_range_rebuild_returns_to_current_view(self):
        response=self.client.post('/admin/rebuild/range',
                                  data={'days':'7','view':'failed','token':self.token()},
                                  follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertEqual(response.headers['location'],'/admin?view=failed')

    def test_unknown_view_falls_back_to_range_default(self):
        response=self.client.post('/admin/rebuild/range',
                                  data={'days':'7','view':'bogus','token':self.token()},
                                  follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertEqual(response.headers['location'],'/admin?view=rebuilds')

    def test_retract_keeps_all_view(self):
        response=self.client.post(f'/admin/tasks/{self.task["id"]}/retract',
                                  data={'view':'all','token':self.token()},
                                  follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertEqual(response.headers['location'],'/admin')

    def test_rebuilds_view_is_not_folded_into_all(self):
        """回归：视图白名单漏了 rebuilds，点“重建记录”会退回“全部任务”。"""
        page=self.client.get('/admin?view=rebuilds')
        self.assertEqual(page.status_code,200)
        self.assertIn('href="/admin?view=rebuilds" data-async-nav aria-current="page"',page.text)
        self.assertIn('<th>日期</th><th>状态</th><th>进度</th><th>耗时</th><th>说明</th>',page.text)
        # 任务视图的“内容/阶段”表头不应出现在重建面板里。
        self.assertNotIn('<th>内容</th><th>状态</th><th>阶段 / 进度</th>',page.text)

    def test_rebuild_action_returns_to_rebuilds_view(self):
        response=self.client.post('/admin/rebuild',data={'day':self.day,'view':'rebuilds','token':self.token()},
                                  follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertEqual(response.headers['location'],'/admin?view=rebuilds')

    def test_cleanup_keeps_current_view(self):
        response=self.client.post('/admin/cleanup',
                                  data={'view':'failed','token':self.token()},
                                  follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertEqual(response.headers['location'],'/admin?view=failed')


if __name__=='__main__':unittest.main()
