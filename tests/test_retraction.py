"""撤回功能：发布过滤、空日报例外、恢复、鉴权与归档死链。"""
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
from integration.cleanup import prune_versions
from integration.files import write_text
from interaction.web import create_app
from orchestration.service import DailyService, day_of


class RetractionTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='retraction-secret-more-than-32-chars',
                               admin_hash=hash_password('abcdefghij'),secure_cookie=False)
        self.service=DailyService(self.settings)
        self.day=day_of(time.time())

    def add_done(self,url,title,bullets=('要点',)):
        self.service.enqueue('rss',{'url':url,'title':title,'published':time.time()})
        task=self.service.store.query('SELECT * FROM tasks WHERE url=?',(url,))[0]
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'summary.json',json.dumps({'topic':'AI 与大模型','bullets':list(bullets)}))
        self.service.store.update(task['id'],status='done',summary_path=str(folder/'summary.json'))
        return task

    def titles(self):
        report=self.service.report(self.day)
        return [e['title'] for e in report['entries']] if report else None

    def test_retract_hides_entry_but_keeps_files_and_versions(self):
        keep=self.add_done('https://example.com/a','保留的文章')
        drop=self.add_done('https://example.com/b','要撤回的文章')
        self.service.publish(self.day)
        self.assertEqual(self.titles(),['保留的文章','要撤回的文章'])
        summary=Path(self.service.store.task(drop['id'])['summary_path'])

        self.service.retract_entry(drop['id'],'来源有误')
        self.assertEqual(self.titles(),['保留的文章'])
        # 撤回不是删除：摘要文件、任务行、历史版本都还在
        self.assertTrue(summary.is_file())
        self.assertEqual(self.service.store.task(drop['id'])['status'],'done')
        self.assertGreaterEqual(len(self.service.store.versions(self.day)),1)
        self.assertIn(drop['id'],self.service.store.retracted_ids(self.day))

    def test_retracted_entry_survives_republish_and_merge_previous(self):
        """回归：_merge_previous 会沿用旧条目，过滤必须在它之后。"""
        keep=self.add_done('https://example.com/a','保留的文章')
        drop=self.add_done('https://example.com/b','要撤回的文章')
        self.service.publish(self.day)
        self.service.retract_entry(drop['id'])
        # 再触发几次发布（含 force），撤回必须一直生效
        for _ in range(3):
            self.service.publish(self.day,force=True)
        self.assertEqual(self.titles(),['保留的文章'])

    def test_retract_all_clears_active_and_removes_archive_link(self):
        drop=self.add_done('https://example.com/b','唯一文章')
        self.service.publish(self.day)
        self.assertIn(self.day,self.service.dates())
        self.assertIsNone(self.service.retract_entry(drop['id']))
        self.assertIsNone(self.service.report(self.day))
        self.assertNotIn(self.day,self.service.dates())
        # 版本行仍在，只是没有活动指针
        self.assertTrue(self.service.store.versions(self.day))
        self.assertIsNone(self.service.store.latest(self.day))

    def test_restore_brings_entry_back(self):
        keep=self.add_done('https://example.com/a','保留的文章')
        drop=self.add_done('https://example.com/b','要撤回的文章')
        self.service.publish(self.day)
        self.service.retract_entry(drop['id'])
        self.assertEqual(self.titles(),['保留的文章'])
        self.service.restore_entry(drop['id'])
        self.assertEqual(self.titles(),['保留的文章','要撤回的文章'])
        self.assertEqual(self.service.store.retracted_ids(self.day),set())

    def test_restore_all_after_full_retraction_reactivates_day(self):
        drop=self.add_done('https://example.com/b','唯一文章')
        self.service.publish(self.day)
        self.service.retract_entry(drop['id'])
        self.assertIsNone(self.service.report(self.day))
        self.service.restore_entry(drop['id'])
        self.assertEqual(self.titles(),['唯一文章'])
        self.assertIn(self.day,self.service.dates())

    def test_invalid_targets_are_rejected(self):
        done=self.add_done('https://example.com/a','文章')
        self.service.enqueue('rss',{'url':'https://example.com/q','title':'排队','published':time.time()})
        queued=self.service.store.query("SELECT * FROM tasks WHERE url='https://example.com/q'")[0]
        self.service.publish(self.day)
        # 排队中的任务不能撤回
        with self.assertRaises(ValueError):
            self.service.retract_entry(queued['id'])
        # 未撤回的任务不能恢复
        with self.assertRaises(ValueError):
            self.service.restore_entry(done['id'])
        self.service.retract_entry(done['id'])
        # 重复撤回是幂等的，不会产生第二条记录
        self.service.retract_entry(done['id'])
        self.assertEqual(len(self.service.store.retracted_ids(self.day)),1)

    def test_retraction_is_audited_in_events(self):
        drop=self.add_done('https://example.com/b','要撤回的文章')
        self.service.publish(self.day)
        self.service.retract_entry(drop['id'],'来源有误')
        self.service.restore_entry(drop['id'])
        events=self.service.store.recent_events(10)
        stages=[(e['stage'],e['status']) for e in events]
        self.assertIn(('retract','ok'),stages)
        self.assertIn(('retract','restored'),stages)
        self.assertTrue(any('来源有误' in (e['detail'] or '') for e in events))

    def test_dashboard_marks_and_filters_retracted(self):
        drop=self.add_done('https://example.com/b','要撤回的文章')
        self.service.publish(self.day)
        self.service.retract_entry(drop['id'])
        rows=self.service.dashboard('done')['tasks']
        self.assertEqual(rows[0]['state_label'],'已撤回')
        self.assertEqual(rows[0]['retracted'],1)
        self.assertEqual(len(self.service.dashboard('retracted')['tasks']),1)
        self.assertEqual(self.service.dashboard('retracted')['task_counts']['retracted'],1)

    def test_retract_does_not_touch_other_days(self):
        """撤回只重算所属日期，其他日期的活动版本不受影响。"""
        today=self.add_done('https://example.com/a','今天的文章')
        self.service.publish(self.day)
        self.service.store.execute("UPDATE tasks SET day='2020-01-01' WHERE id=?",(today['id'],))
        self.service.publish('2020-01-01')
        # 原日期已无任务，发布不会新建版本；直接确认两个日期的活动状态
        self.service.store.execute('UPDATE versions SET active=0 WHERE day=?',(self.day,))
        self.assertEqual(self.service.dates(),['2020-01-01'])
        self.service.retract_entry(today['id'])
        self.assertIsNone(self.service.report('2020-01-01'))
        self.assertNotIn('2020-01-01',self.service.dates())


class RetractionRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='retraction-route-secret-32-chars-min',
                               admin_hash=hash_password('abcdefghij'),secure_cookie=False)
        self.app=create_app(self.settings,run_background=False)
        self.service=self.app.state.service
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'文章','published':time.time()})
        task=self.service.store.query('SELECT * FROM tasks')[0]
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'summary.json',json.dumps({'topic':'AI 与大模型','bullets':['要点']}))
        self.service.store.update(task['id'],status='done',summary_path=str(folder/'summary.json'))
        self.task_id=task['id']
        self.day=task['day']
        self.service.publish(self.day)

    def csrf(self):
        page=self.client.get('/admin')
        return re.search('name="token" value="([^"]+)"',page.text)[1]

    def test_requires_login(self):
        response=self.client.post(f'/admin/tasks/{self.task_id}/retract',data={'token':'x'*43})
        self.assertEqual(response.status_code,401)
        self.assertEqual(self.service.store.retracted_ids(self.day),set())

    def test_requires_csrf(self):
        self.client.cookies.set('daily_session',self.service.new_session(True))
        response=self.client.post(f'/admin/tasks/{self.task_id}/retract',data={'token':'wrong'})
        self.assertEqual(response.status_code,403)
        self.assertEqual(self.service.store.retracted_ids(self.day),set())

    def test_retract_and_restore_over_http(self):
        self.client.cookies.set('daily_session',self.service.new_session(True))
        token=self.csrf()
        response=self.client.post(f'/admin/tasks/{self.task_id}/retract',
                                  data={'token':token,'reason':'来源有误'},follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertIsNone(self.service.report(self.day))
        response=self.client.post(f'/admin/tasks/{self.task_id}/restore',data={'token':token},follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertEqual(len(self.service.report(self.day)['entries']),1)

    def test_retract_unknown_task_returns_409(self):
        self.client.cookies.set('daily_session',self.service.new_session(True))
        token=self.csrf()
        response=self.client.post('/admin/tasks/99999/retract',data={'token':token})
        self.assertEqual(response.status_code,409)

    def test_admin_page_shows_retract_and_restore_controls(self):
        self.client.cookies.set('daily_session',self.service.new_session(True))
        page=self.client.get('/admin?view=done').text
        self.assertIn(f'/admin/tasks/{self.task_id}/retract',page)
        self.assertIn('撤回',page)
        token=self.csrf()
        self.client.post(f'/admin/tasks/{self.task_id}/retract',data={'token':token})
        page=self.client.get('/admin?view=retracted').text
        self.assertIn(f'/admin/tasks/{self.task_id}/restore',page)
        self.assertIn('已撤回',page)
        self.assertNotIn(f'/admin/tasks/{self.task_id}/retract',page)


class ArchiveDeadLinkTests(unittest.TestCase):
    """先前发现的两处：dates() 死链、活动行指向丢失文件。"""

    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='dead-link-secret-more-than-32-chars',
                               admin_hash='offline-hash',secure_cookie=False)
        self.service=DailyService(self.settings)
        self.day=day_of(time.time())
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'文章','published':time.time()})
        task=self.service.store.query('SELECT * FROM tasks')[0]
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'summary.json',json.dumps({'topic':'AI 与大模型','bullets':['要点']}))
        self.service.store.update(task['id'],status='done',summary_path=str(folder/'summary.json'))
        self.service.publish(self.day)

    def test_dates_only_lists_days_with_active_version(self):
        self.assertEqual(self.service.dates(),[self.day])
        self.service.store.execute('UPDATE versions SET active=0')
        self.assertEqual(self.service.dates(),[])

    def test_lost_active_file_is_demoted_so_navigation_has_no_dead_link(self):
        active=self.service.store.latest(self.day)
        Path(active['path']).unlink()
        prune_versions(self.settings,self.settings.data_dir.resolve(),time.time())
        self.assertIsNone(self.service.store.latest(self.day),'活动指针应被降级')
        self.assertNotIn(self.day,self.service.dates(),'归档导航不应列出死链')
        # 下一次发布能重建并重新激活
        self.service.publish(self.day)
        self.assertIsNotNone(self.service.store.latest(self.day))
        self.assertIn(self.day,self.service.dates())


if __name__=='__main__':unittest.main()
