"""范围重建：按日展开、整批只扫一次来源、跳过空白日期、逐日发布。"""
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
from integration.files import write_text
from interaction.web import create_app
from orchestration.service import DailyService, day_of


class RangeRebuildTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='range-rebuild-secret-more-than-32-chars',
                               admin_hash=hash_password('abcdefghij'),secure_cookie=False)
        self.service=DailyService(self.settings)
        self.pipeline=self.service._ensure_pipeline()
        self.now=time.time()

    def seed(self,offset,url=None):
        """造一条当日已完成并已发布的任务，产物完整以免被判为需补齐。"""
        day=day_of(self.now-offset*86400)
        url=url or f'https://example.com/{offset}'
        self.service.enqueue('rss',{'url':url,'title':f'第{offset}天','published':time.time()})
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
        return day,task

    def test_range_expands_to_per_day_entries_with_shared_batch(self):
        days=[self.seed(offset)[0] for offset in (0,1,2)]
        queued=self.service.request_rebuild_range(7)
        self.assertEqual(sorted(queued),sorted(days),'只应入队有内容的日期')
        rows=self.service.store.query('SELECT day,batch FROM rebuilds')
        batches={row['batch'] for row in rows}
        self.assertEqual(len(batches),1,'同一次提交共用一个批次号')
        self.assertTrue(batches.pop().startswith('range-'))

    def test_empty_days_are_not_queued(self):
        self.seed(0)
        queued=self.service.request_rebuild_range(7)
        self.assertEqual(len(queued),1)
        self.assertEqual(len(self.service.store.query('SELECT id FROM rebuilds')),1)

    def test_no_content_returns_empty_instead_of_failing(self):
        self.assertEqual(self.service.request_rebuild_range(7),[])
        self.assertEqual(self.service.store.query('SELECT id FROM rebuilds'),[])

    def test_days_out_of_supported_range_are_rejected(self):
        for value in (0,-1,31,999):
            with self.subTest(value=value),self.assertRaises(ValueError):
                self.service.request_rebuild_range(value)

    def test_source_is_scanned_once_per_batch(self):
        """回归：逐日调用 poll 会把每个来源重复扫描 N 遍。"""
        for offset in (0,1,2):
            self.seed(offset)
        self.service.request_rebuild_range(7)
        calls=[]
        real=self.service.poll
        def spy(day=None,end_day=None,parent=''):
            calls.append((day,end_day))
            return real(day=day,end_day=end_day,parent=parent)
        self.service.poll=spy
        with patch('orchestration.service.discover',return_value=[]):
            while self.service.process_rebuild():
                pass
        self.assertEqual(len(calls),1,'三天应只扫描一次来源')
        start,end=calls[0]
        self.assertLess(start,end,'区间模式应传起始与结束日期')

    def test_batch_completes_all_days_and_keeps_reports(self):
        days=[self.seed(offset)[0] for offset in (0,1,2)]
        self.service.request_rebuild_range(7)
        with patch('orchestration.service.discover',return_value=[]):
            while self.service.process_rebuild():
                pass
        rows=self.service.store.query('SELECT day,status FROM rebuilds')
        self.assertTrue(all(row['status']=='done' for row in rows),rows)
        for day in days:
            self.assertIsNotNone(self.service.report(day))
            self.assertIn(day,self.service.dates())

    def test_batch_progress_reports_counts(self):
        for offset in (0,1):
            self.seed(offset)
        self.service.request_rebuild_range(3)
        batch=self.service.store.query('SELECT batch FROM rebuilds LIMIT 1')[0]['batch']
        with patch('orchestration.service.discover',return_value=[]):
            while self.service.process_rebuild():
                pass
        progress=self.service.store.batch_progress(batch)
        self.assertEqual(progress['total'],2)
        self.assertEqual(progress['done'],2)
        self.assertEqual(progress['queued'],0)

    def test_scan_marker_is_cleared_after_batch(self):
        for offset in (0,1):
            self.seed(offset)
        self.service.request_rebuild_range(3)
        with patch('orchestration.service.discover',return_value=[]):
            while self.service.process_rebuild():
                pass
        self.assertEqual(self.service.store.query("SELECT key FROM meta WHERE key LIKE 'rebuild_scan:%'"),[])

    def test_single_day_rebuild_is_unaffected(self):
        day,_=self.seed(0)
        self.assertTrue(self.service.request_rebuild(day))
        it=self.service.store.claim_rebuild()
        self.assertEqual(it['batch'],'','单日重建不带批次')
        with patch('orchestration.service.discover',return_value=[]):
            self.service.process_rebuild()

    def test_crash_recovery_rescans_sources(self):
        """回归：批次中途崩溃后沿用旧扫描标记会静默漏掉新内容。"""
        for offset in (0,1):
            self.seed(offset)
        self.service.request_rebuild_range(3)
        request=self.service.store.claim_rebuild()
        with patch('orchestration.service.discover',return_value=[]):
            self.service._rebuild_inspection(request,request['day'])
        marker=f"rebuild_scan:{request['batch']}"
        self.assertTrue(self.service.store.meta(marker),'首轮应写入扫描标记')
        self.service.store.recover()
        self.assertEqual(self.service.store.meta(marker),'','恢复后必须清除标记以便重新扫描')
        calls=[]
        real=self.service.poll
        def spy(day=None,end_day=None,parent=''):
            calls.append((day,end_day))
            return real(day=day,end_day=end_day,parent=parent)
        self.service.poll=spy
        with patch('orchestration.service.discover',return_value=[]):
            while self.service.process_rebuild():
                pass
        self.assertEqual(len(calls),1,'恢复后应重新扫描一次来源')
        rows=self.service.store.query('SELECT status FROM rebuilds')
        self.assertTrue(all(row['status']=='done' for row in rows))

    def test_duplicate_range_submission_skips_pending_days(self):
        self.seed(0)
        self.assertEqual(len(self.service.request_rebuild_range(3)),1)
        self.assertEqual(self.service.request_rebuild_range(3),[],'已在队列中的日期不重复入队')


class RangeRebuildRouteTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='range-route-secret-more-than-32-chars',
                               admin_hash=hash_password('abcdefghij'),secure_cookie=False)
        self.app=create_app(self.settings,run_background=False)
        self.service=self.app.state.service
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)
        self.pipeline=self.service._ensure_pipeline()
        day=day_of(time.time())
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'x','published':time.time()})
        task=self.service.store.query('SELECT * FROM tasks')[0]
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'raw.txt','正文')
        write_text(folder/'summary.json',json.dumps({'topic':'AI 与大模型','bullets':['b']}))
        self.service.store.update(task['id'],status='done',summary_path=str(folder/'summary.json'),
                                  raw_path=str(folder/'raw.txt'))
        self.service.publish(day)
        self.client.cookies.set('daily_session',self.service.new_session(True))

    def token(self):
        return re.search('name="token" value="([^"]+)"',self.client.get('/admin').text)[1]

    def test_route_queues_range(self):
        response=self.client.post('/admin/rebuild/range',data={'days':'7','token':self.token()},
                                  follow_redirects=False)
        self.assertEqual(response.status_code,303)
        self.assertEqual(len(self.service.store.query('SELECT id FROM rebuilds')),1)

    def test_route_requires_login_and_csrf(self):
        from fastapi.testclient import TestClient
        anonymous=TestClient(self.app)
        self.addCleanup(anonymous.close)
        # 未登录：带 token 也拿不到会话 -> 401
        self.assertEqual(anonymous.post('/admin/rebuild/range',data={'days':'7','token':'x'*43}).status_code,401)
        # 已登录但 token 错误 -> 403
        self.assertEqual(self.client.post('/admin/rebuild/range',data={'days':'7','token':'wrong'}).status_code,403)

    def test_invalid_days_are_rejected(self):
        token=self.token()
        for value in ('0','99','abc'):
            with self.subTest(value=value):
                response=self.client.post('/admin/rebuild/range',data={'days':value,'token':token})
                self.assertEqual(response.status_code,400)
        # 空值由 FastAPI 表单校验拦截，同样不进入业务
        self.assertEqual(self.client.post('/admin/rebuild/range',data={'days':'','token':token}).status_code,422)

    def test_empty_range_returns_409(self):
        self.service.store.execute("DELETE FROM tasks")
        self.service.store.execute("DELETE FROM versions")
        token=self.token()
        response=self.client.post('/admin/rebuild/range',data={'days':'7','token':token})
        self.assertEqual(response.status_code,409)

    def test_page_offers_range_control(self):
        page=self.client.get('/admin').text
        self.assertIn('/admin/rebuild/range',page)
        self.assertIn('最近 7 天',page)
        self.assertIn('最近 30 天',page)


if __name__=='__main__':unittest.main()
