"""重试上限：自动重试有界、手动重试不重置、终身累计封顶、重建不刷新额度。"""
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from config import Settings
from integration.auth import hash_password
from integration.cleanup import cleanup
from integration.files import write_text
from interaction.web import create_app
from orchestration.pipeline import ContentPipeline
from orchestration.service import DailyService, day_of


class RetryLimitTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='retry-limit-secret-more-than-32-chars',
                               admin_hash=hash_password('abcdefghij'),secure_cookie=False)
        self.service=DailyService(self.settings)
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'总失败','published':time.time()})
        self.task=self.service.store.query('SELECT * FROM tasks')[0]
        self.pipeline=self.service._ensure_pipeline()

    def fail_once(self,task=None):
        task=task or self.service.store.task(self.task['id'])
        with patch('orchestration.pipeline.extract',side_effect=RuntimeError('offline')):
            self.pipeline.run(task)

    def drain(self,limit=50):
        """跑完所有可认领的尝试，模拟 Worker 连续轮次。"""
        runs=0
        while runs<limit:
            self.service.store.execute("UPDATE tasks SET available=0 WHERE status='queued'")
            task=self.service.store.claim()
            if task is None:break
            self.fail_once(task)
            runs+=1
        return runs

    def test_automatic_retries_are_bounded(self):
        runs=self.drain()
        row=self.service.store.task(self.task['id'])
        self.assertEqual(runs,3,'max_attempts=3 应只执行 3 次')
        self.assertEqual(row['status'],'failed')
        self.assertEqual(row['attempts'],3)
        self.assertIsNone(self.service.store.claim(),'失败后不再被自动认领')

    def test_manual_retry_does_not_reset_counter(self):
        self.drain()
        self.service.retry(self.task['id'])
        row=self.service.store.task(self.task['id'])
        self.assertEqual(row['status'],'queued')
        self.assertEqual(row['attempts'],3,'累加次数必须保留')

    def test_manual_retry_is_capped_at_lifetime_limit(self):
        """回归：反复点重试曾能无限产生新的 3 次尝试。"""
        self.drain()
        clicks=0
        while clicks<50:
            try:
                self.service.retry(self.task['id'])
            except ValueError:
                break
            clicks+=1
            self.drain()
        row=self.service.store.task(self.task['id'])
        self.assertGreaterEqual(row['attempts'],10)
        self.assertEqual(row['status'],'failed')
        self.assertLess(clicks,15,'重试次数必须被封顶')

    def test_retry_rejected_at_limit_with_actionable_message(self):
        self.service.store.execute("UPDATE tasks SET status='failed',attempts=10 WHERE id=?",(self.task['id'],))
        with self.assertRaises(ValueError) as context:
            self.service.retry(self.task['id'])
        self.assertIn('达到上限',str(context.exception))
        self.assertIn('重建',str(context.exception))
        self.assertEqual(self.service.store.task(self.task['id'])['status'],'failed')

    def test_retry_still_allowed_below_limit(self):
        self.service.store.execute("UPDATE tasks SET status='failed',attempts=5 WHERE id=?",(self.task['id'],))
        self.service.retry(self.task['id'])
        self.assertEqual(self.service.store.task(self.task['id'])['status'],'queued')

    def test_task_state_reports_exhausted(self):
        self.service.store.execute("UPDATE tasks SET status='failed',attempts=10 WHERE id=?",(self.task['id'],))
        rows=self.service.dashboard('failed')['tasks']
        self.assertTrue(rows[0]['exhausted'])
        self.assertIn('终身尝试上限',rows[0]['state_note'])

    def test_task_state_reports_retryable(self):
        self.service.store.execute("UPDATE tasks SET status='failed',attempts=3 WHERE id=?",(self.task['id'],))
        rows=self.service.dashboard('failed')['tasks']
        self.assertFalse(rows[0]['exhausted'])
        self.assertIn('可手动重试',rows[0]['state_note'])

    def test_rebuild_skips_task_at_lifetime_limit(self):
        """重建不应给已达上限的任务一份全新的自动重试额度。"""
        day=self.task['day']
        self.service.store.execute("UPDATE tasks SET status='failed',attempts=10,day=? WHERE id=?",(day,self.task['id']))
        self.service.request_rebuild(day)
        with patch('orchestration.service.discover',return_value=[]):
            self.service.process_rebuild()
        row=self.service.store.task(self.task['id'])
        self.assertEqual(row['status'],'failed','达到上限的任务不应被重建重新排队')
        self.assertEqual(row['attempts'],10)
        self.assertEqual(self.service.store.query('SELECT id FROM tasks WHERE day=? AND status=?',(day,'queued')),[])

    def test_rebuild_still_requeues_failed_task_below_limit(self):
        day=self.task['day']
        self.service.store.execute("UPDATE tasks SET status='failed',attempts=3,day=? WHERE id=?",(day,self.task['id']))
        self.service.request_rebuild(day)
        with patch('orchestration.service.discover',return_value=[]), \
             patch('orchestration.pipeline.ContentPipeline.repair_needed',return_value=True):
            self.service.process_rebuild()
        row=self.service.store.task(self.task['id'])
        self.assertIn(row['status'],('failed','done','queued'))
        self.assertNotEqual(row['attempts'],0,'重建不应把尝试次数清零')


class RetryRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='retry-route-secret-more-than-32-chars',
                               admin_hash=hash_password('abcdefghij'),secure_cookie=False)
        self.app=create_app(self.settings,run_background=False)
        self.service=self.app.state.service
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'x','published':time.time()})
        self.task_id=self.service.store.query('SELECT * FROM tasks')[0]['id']
        self.client.cookies.set('daily_session',self.service.new_session(True))

    def token(self):
        return re.search('name="token" value="([^"]+)"',self.client.get('/admin').text)[1]

    def test_route_returns_409_when_exhausted(self):
        self.service.store.execute("UPDATE tasks SET status='failed',attempts=10 WHERE id=?",(self.task_id,))
        response=self.client.post(f'/admin/tasks/{self.task_id}/retry',data={'token':self.token()})
        self.assertEqual(response.status_code,409)
        self.assertIn('达到上限',response.text)

    def test_page_hides_retry_button_when_exhausted(self):
        self.service.store.execute("UPDATE tasks SET status='failed',attempts=10 WHERE id=?",(self.task_id,))
        page=self.client.get('/admin?view=failed').text
        self.assertNotIn(f'/admin/tasks/{self.task_id}/retry',page)
        self.assertIn('已达上限',page)

    def test_page_shows_retry_button_below_limit(self):
        self.service.store.execute("UPDATE tasks SET status='failed',attempts=2 WHERE id=?",(self.task_id,))
        page=self.client.get('/admin?view=failed').text
        self.assertIn(f'/admin/tasks/{self.task_id}/retry',page)


class RetryConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config=Path(self.directory.name)/'config.toml'
        from config import ROOT
        self.template=(ROOT/'config.example.toml').read_text(encoding='utf-8')
        self.config.write_text(self.template,encoding='utf-8')

    def test_default_and_legacy_fallback(self):
        self.assertEqual(Settings(config_file=self.config).get('worker','max_total_attempts'),10)
        legacy='\n'.join(line for line in self.template.splitlines() if not line.startswith('max_total_attempts'))
        self.config.write_text(legacy,encoding='utf-8')
        self.assertEqual(Settings(config_file=self.config).get('worker','max_total_attempts',10),10)

    def test_invalid_values_rejected(self):
        for value in ('0','1','101','true','"10"'):
            self.config.write_text(self.template.replace('max_total_attempts = 10',f'max_total_attempts = {value}'),encoding='utf-8')
            with self.subTest(value=value),self.assertRaises(ValueError):
                Settings(config_file=self.config)


if __name__=='__main__':unittest.main()
