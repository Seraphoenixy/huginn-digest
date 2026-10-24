"""管理页新增能力：ASR 状态、撤回原因、存储统计、手动清理。"""
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
from integration.cleanup import disk_usage
from integration.files import write_text
from integration.store import Store
from interaction.web import create_app
from orchestration.service import DailyService, day_of


class StorageUsageTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='usage-secret-more-than-32-characters',
                               admin_hash='offline-hash',secure_cookie=False)
        Store(self.settings.db_path)

    def test_buckets_are_classified_exactly(self):
        folder=self.settings.content_dir/'1'
        write_text(folder/'raw.txt','A'*1000)
        write_text(folder/'corrected.txt','B'*1000)
        write_text(folder/'part_1.txt','C'*500)
        write_text(folder/'summary.json',json.dumps({'topic':'AI 与大模型','bullets':['x']}))
        write_text(self.settings.cache_dir/'downloads'/'a.bin','D'*300)
        write_text(self.settings.data_dir/'reports'/'2026-01-01'/'v.json','E'*200)
        usage=disk_usage(self.settings)
        self.assertEqual(usage['buckets']['transcripts'],2500)
        self.assertEqual(usage['buckets']['cache'],300)
        self.assertEqual(usage['buckets']['reports'],200)
        self.assertEqual(usage['counts']['transcripts'],3)
        self.assertGreater(usage['buckets']['summaries'],0)

    def test_database_includes_wal_and_is_not_zero(self):
        """回归：只匹配主文件会把 WAL 模式下的数据库算成 0。"""
        usage=disk_usage(self.settings)
        self.assertGreater(usage['buckets']['database'],0)
        self._seed()
        self.assertGreater(disk_usage(self.settings)['buckets']['database'],0)

    def _seed(self):
        service=DailyService(self.settings)
        service.enqueue('rss',{'url':'https://example.com/a','title':'x','published':time.time()})
        service.store.claim()

    def test_total_matches_sum_of_buckets(self):
        write_text(self.settings.content_dir/'1'/'raw.txt','A'*100)
        usage=disk_usage(self.settings)
        self.assertEqual(usage['total'],sum(usage['buckets'].values()))

    def test_missing_data_dir_is_safe(self):
        usage=disk_usage(Settings(data_dir=Path(self.directory.name)/'missing',
                                  session_secret='x'*40,admin_hash='x',secure_cookie=False))
        self.assertEqual(usage['total'],0)


class AdminPanelTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='panel-secret-more-than-32-characters',
                               admin_hash=hash_password('abcdefghij'),secure_cookie=False)
        self.app=create_app(self.settings,run_background=False)
        self.service=self.app.state.service
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'文章','published':time.time()})
        task=self.service.store.query('SELECT * FROM tasks')[0]
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'raw.txt','转写'*2000)
        write_text(folder/'summary.json',json.dumps({'topic':'AI 与大模型','bullets':['要点']},ensure_ascii=False))
        self.service.store.update(task['id'],status='done',summary_path=str(folder/'summary.json'),
                                  raw_path=str(folder/'raw.txt'))
        self.task_id=task['id']
        self.day=task['day']
        self.service.publish(self.day)
        self.client.cookies.set('daily_session',self.service.new_session(True))

    def token(self):
        return re.search('name="token" value="([^"]+)"',self.client.get('/admin').text)[1]

    def test_asr_state_is_visible_again(self):
        """回归：ASR 配置状态在改版时从模板里丢失。"""
        page=self.client.get('/admin').text
        self.assertIn('转写',page)
        # 夹具未配置 asr_key
        self.assertIn('未配置转写 Key',page)

    def test_asr_warning_disappears_when_configured(self):
        settings=Settings(data_dir=Path(self.directory.name)/'configured',
                          session_secret='panel-secret-more-than-32-characters',
                          admin_hash=hash_password('abcdefghij'),secure_cookie=False)
        settings.asr_key='fixture-asr-key'
        app=create_app(settings,run_background=False)
        with TestClient(app) as client:
            client.cookies.set('daily_session',app.state.service.new_session(True))
            page=client.get('/admin').text
            self.assertNotIn('未配置转写 Key',page)
            self.assertIn('转写已配置',page)

    def test_storage_card_shows_every_bucket_so_the_total_adds_up(self):
        """回归：漏掉 other 分类时，各行相加凑不出合计。"""
        page=self.client.get('/admin').text
        for text in ('存储与保留','合计','转写原文','摘要与快照','已发布日报','下载缓存',
                     '数据库','其他文件','下次自动清理','保留期：'):
            self.assertIn(text,page)
        self.assertIn('日报 1095 天',page)
        match=re.search(r'合计</span><span class="settings-row__value">([\d.]+) MB',page)
        self.assertIsNotNone(match)
        self.assertGreater(float(match[1]),0)

    def test_bucket_sum_equals_total(self):
        folder=self.settings.content_dir/'9'
        write_text(folder/'raw.txt','A'*1000)
        write_text(folder/'summary.json',json.dumps({'topic':'AI 与大模型','bullets':['x']}))
        write_text(self.settings.data_dir/'stray.png','P'*700)
        usage=disk_usage(self.settings)
        self.assertEqual(usage['buckets']['other'],700,'根目录下的非归类文件必须计入 other')
        self.assertEqual(usage['total'],sum(usage['buckets'].values()),
                         '合计必须等于各分类之和，页面才不会出现缺口')

    def test_retraction_reason_is_visible(self):
        token=self.token()
        self.client.post(f'/admin/tasks/{self.task_id}/retract',
                         data={'token':token,'reason':'版权方要求'})
        page=self.client.get('/admin?view=retracted').text
        self.assertIn('版权方要求',page)
        self.assertIn('已撤回',page)

    def test_manual_cleanup_runs_and_records_result(self):
        token=self.token()
        response=self.client.post('/admin/cleanup',data={'token':token},follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertGreater(float(self.service.store.meta('cleanup_at')),0)
        page=self.client.get('/admin').text
        self.assertIn('上次清理',page)
        self.assertNotIn('上次清理失败',page)

    def test_manual_cleanup_removes_expired_transcripts(self):
        folder=self.settings.content_dir/str(self.task_id)
        ancient=time.time()-400*86400
        import os
        os.utime(folder/'raw.txt',(ancient,ancient))
        token=self.token()
        self.client.post('/admin/cleanup',data={'token':token})
        self.assertFalse((folder/'raw.txt').exists())
        self.assertTrue((folder/'summary.json').is_file())
        # 日报不受影响
        self.assertIsNotNone(self.service.report(self.day))

    def test_manual_cleanup_requires_login_and_csrf(self):
        anonymous=TestClient(self.app)
        self.addCleanup(anonymous.close)
        self.assertEqual(anonymous.post('/admin/cleanup',data={'token':'x'*43}).status_code,401)
        self.assertEqual(self.client.post('/admin/cleanup',data={'token':'wrong'}).status_code,403)

    def test_cleanup_failure_is_surfaced(self):
        self.service.store.set_meta('cleanup_error','OSError')
        page=self.client.get('/admin').text
        self.assertIn('上次清理失败',page)

    def test_cleanup_failure_returns_409_without_raising(self):
        with patch('integration.cleanup.cleanup',side_effect=OSError('boom')):
            token=self.token()
            response=self.client.post('/admin/cleanup',data={'token':token})
        self.assertEqual(response.status_code,409)
        self.assertIn('清理未完成',response.text)

    def test_cleanup_failure_does_not_break_later_ticks(self):
        with patch('integration.cleanup.cleanup',side_effect=OSError('boom')):
            self.assertIsNone(self.service.run_cleanup())
        self.assertEqual(self.service.store.meta('cleanup_error'),'OSError')
        with patch('integration.cleanup.cleanup',return_value={'files':1,'bytes':10,'remaining_bytes':0}):
            self.assertIsNotNone(self.service.run_cleanup())
        self.assertEqual(self.service.store.meta('cleanup_error'),'')


if __name__=='__main__':unittest.main()
