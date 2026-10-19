import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, Mock
from fastapi.testclient import TestClient
from config import Settings
from integration.auth import hash_password
from integration.files import write_text
from orchestration.service import DailyService,day_of
from interaction.web import create_app

class RebuildTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.settings=Settings(data_dir=Path(self.directory.name),session_secret='rebuild-test-secret-more-than-32-characters',admin_hash=hash_password('abcdefghij'),secure_cookie=False)
        self.service=DailyService(self.settings)
        self.day=day_of(time.time())
        self.service.enqueue('rss',{'url':'https://example.com/item','title':'test','published':time.time()})
        task=self.service.store.claim()
        path=self.settings.content_dir/str(task['id'])/'summary.json'
        write_text(path,json.dumps({'topic':'技术','bullets':['test']}))
        write_text(path.parent/'raw.txt','测试正文')
        from orchestration.pipeline import ContentPipeline
        pipeline=ContentPipeline(self.settings,self.service.store)
        write_text(path.parent/'summary_manifest.json',json.dumps({'fingerprint':pipeline.summary_fingerprint(path.parent/'raw.txt')}))
        self.service.store.update(task['id'],status='done',summary_path=str(path))
        self.service.publish(self.day)

    def tearDown(self):self.directory.cleanup()

    def test_http_only_queues_worker_executes_and_deduplicates(self):
        app=create_app(self.settings, run_background=False)
        with TestClient(app) as client:
            page=client.get('/admin/login')
            csrf=re.search('name="token" value="([^"]+)"',page.text)[1]
            dashboard=client.post('/admin/login',data={'password':'abcdefghij','token':csrf})
            csrf=re.search('name="token" value="([^"]+)"',dashboard.text)[1]
            with patch.object(app.state.service,'publish',side_effect=AssertionError('Web must not rebuild')):
                response=client.post('/admin/rebuild',data={'day':self.day,'token':csrf})
                self.assertEqual(response.status_code,200)
            self.assertEqual(self.service.dashboard()['rebuilds'][0]['status'],'queued')
            self.assertFalse(self.service.request_rebuild(self.day))
            worker=DailyService(self.settings)
            worker.tick()
            self.assertEqual(self.service.dashboard()['rebuilds'][0]['status'],'done')
            self.assertEqual(len(self.service.store.query('SELECT id FROM versions')),1)

    def test_rebuild_failure_keeps_published_version_and_allows_resubmit(self):
        previous=self.service.store.latest(self.day)
        self.service.request_rebuild(self.day)
        with patch.object(self.service,'publish',side_effect=RuntimeError('private-error')):
            self.service.process_rebuild()
        job=self.service.dashboard()['rebuilds'][0]
        self.assertEqual(job['status'],'failed')
        self.assertNotIn('private-error',job['error'])
        self.assertEqual(previous,self.service.store.latest(self.day))
        self.assertTrue(self.service.request_rebuild(self.day))

    def test_running_rebuild_recovers_and_empty_day_can_be_requested(self):
        self.service.request_rebuild(self.day)
        self.service.store.claim_rebuild()
        self.assertIsNone(self.service.store.claim_rebuild())
        self.service.store.recover()
        self.assertIsNotNone(self.service.store.claim_rebuild())
        self.assertTrue(self.service.request_rebuild('2000-01-01'))

    def test_missing_summary_calls_model_but_complete_result_reused(self):
        from orchestration.pipeline import ContentPipeline
        task=self.service.store.query('SELECT * FROM tasks')[0]
        Path(task['summary_path']).unlink()
        pipeline=ContentPipeline(self.settings,self.service.store)
        pipeline.llm.call=Mock(return_value=json.dumps({'topic':'技术','bullets':['修复后的摘要']}))
        self.service.pipeline=pipeline
        self.service.request_rebuild(self.day)
        self.service.process_rebuild()
        pipeline.llm.call.assert_called_once()
        self.assertEqual(self.service.dashboard()['rebuilds'][0]['status'],'done')
        self.assertEqual(self.service.report(self.day)['entries'][0]['bullets'],['修复后的摘要'])
        pipeline.llm.call.reset_mock()
        self.service.request_rebuild(self.day);self.service.process_rebuild()
        pipeline.llm.call.assert_not_called()

    def test_missing_raw_is_recollected(self):
        from orchestration.pipeline import ContentPipeline
        task=self.service.store.query('SELECT * FROM tasks')[0]
        folder=Path(task['summary_path']).parent
        (folder/'raw.txt').unlink()
        pipeline=ContentPipeline(self.settings,self.service.store)
        pipeline.llm.call=Mock(return_value=json.dumps({'topic':'技术','bullets':['更新后的摘要']}))
        self.service.pipeline=pipeline
        self.service.request_rebuild(self.day)
        with patch('orchestration.pipeline.extract',return_value=('重新获取正文','')) as extract:
            self.service.process_rebuild()
        extract.assert_called_once()
        self.assertEqual((folder/'raw.txt').read_text(encoding='utf-8'),'重新获取正文')
        self.assertEqual(self.service.dashboard()['rebuilds'][0]['status'],'done')

    def test_date_scoped_source_discovery_ignores_poll_interval(self):
        self.service.add_source('bili','123','test')
        self.service.store.execute('UPDATE sources SET last_poll=?',(time.time(),))
        source=Mock()
        source.fetch_page.return_value=([{'bvid':'BV1SRnM6FEmo','created':time.time(),'title':'same day'},{'bvid':'old','created':time.time()-3*86400,'title':'old'}],2)
        with patch.object(self.service,'bili',return_value=source):
            result=self.service.poll(day=self.day)
        self.assertEqual(result['errors'],[])
        tasks=self.service.store.query('SELECT * FROM tasks')
        self.assertEqual(len(tasks),2)
        self.assertTrue(all(t['day']==self.day for t in tasks))

    def test_source_failure_does_not_report_rebuild_success(self):
        from integration.sources.bili import BiliSourceError
        self.service.add_source('bili','123','test')
        source=Mock();source.fetch_page.side_effect=BiliSourceError(-352,False)
        self.service.request_rebuild(self.day)
        with patch.object(self.service,'bili',return_value=source):self.service.process_rebuild()
        self.assertEqual(self.service.dashboard()['rebuilds'][0]['status'],'failed')
        self.assertIn('来源失败 1',self.service.dashboard()['rebuilds'][0]['error'])
        self.assertIsNotNone(self.service.report(self.day))
