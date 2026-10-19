import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from config import Settings
from integration.files import write_text
from integration.auth import hash_password
from orchestration.service import DailyService, day_of, is_final
from orchestration.pipeline import ContentPipeline
from fastapi.testclient import TestClient
from interaction.web import create_app

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.settings=Settings(data_dir=Path(self.directory.name),session_secret='test-session-secret-32-characters-long',admin_hash=hash_password('correct-test-password'),secure_cookie=False)
        self.service=DailyService(self.settings)

    def tearDown(self):self.directory.cleanup()

    def item(self,url='https://example.com/article'):
        return {'url':url,'title':'测试资讯','published':time.time()}

    def done(self,url='https://example.com/article'):
        self.service.enqueue('rss',self.item(url))
        task=self.service.store.query('SELECT * FROM tasks WHERE url=?',(url,))[0]
        path=self.settings.content_dir/str(task['id'])/'summary.json'
        write_text(path,json.dumps({'topic':'技术','bullets':['事实要点']}))
        self.service.store.update(task['id'],status='done',summary_path=str(path))
        return task

    def test_dedup_claim_and_recovery(self):
        self.assertTrue(self.service.enqueue('rss',self.item()))
        self.assertFalse(self.service.enqueue('rss',self.item()))
        task=self.service.store.claim()
        self.assertIsNone(self.service.store.claim())
        self.service.store.recover()
        self.assertEqual(self.service.store.claim()['id'],task['id'])

    def test_source_name_snapshot_legacy_fallback_and_html_escape(self):
        task=self.done()
        source_id=self.service.add_source('bili','123','<来源名称>')
        self.service.store.execute('UPDATE tasks SET source_id=? WHERE id=?',(source_id,task['id']))
        self.service.publish(task['day'])
        self.assertEqual(self.service.report(task['day'])['entries'][0]['source_name'],'<来源名称>')
        with TestClient(create_app(self.settings, run_background=False)) as client:
            page=client.get('/').text
            self.assertIn('&lt;来源名称&gt;',page)
            from html.parser import HTMLParser
            class SummaryParser(HTMLParser):
                inside=False
                text=''
                def handle_starttag(self,tag,attrs):
                    if tag=='summary':self.inside=True
                def handle_endtag(self,tag):
                    if tag=='summary':self.inside=False
                def handle_data(self,data):
                    if self.inside:self.text+=data
            parser=SummaryParser();parser.feed(page)
            self.assertIn('来源：<来源名称>',parser.text)
            self.assertIn('查看原文',page)
        self.service.store.execute('DELETE FROM sources WHERE id=?',(source_id,))
        self.assertEqual(self.service.report(task['day'])['entries'][0]['source_name'],'<来源名称>')
        row=self.service.store.latest(task['day'])
        payload=json.loads(Path(row['path']).read_text(encoding='utf-8'))
        del payload['entries'][0]['source_name']
        write_text(Path(row['path']),json.dumps(payload))
        self.assertEqual(self.service.report(task['day'])['entries'][0]['source_name'],'已删除的订阅来源')
        self.assertEqual(self.service.source_label(dict(task,source_id=None,kind='bili')),'B站 · 单独提交')

    def test_pipeline_uses_files_and_reuses_outputs(self):
        self.service.enqueue('rss',self.item())
        task=self.service.store.claim()
        pipeline=ContentPipeline(self.settings,self.service.store)
        pipeline.llm.call=Mock(return_value=json.dumps({'topic':'技术','bullets':['测试']}))
        with patch('orchestration.pipeline.extract',return_value=('原始正文','')):
            pipeline.run(task)
        self.assertEqual(self.service.store.task(task['id'])['status'],'done')
        pipeline.llm.call.reset_mock()
        with patch('orchestration.pipeline.extract',side_effect=AssertionError('重复采集')):
            result=pipeline.graph.invoke({'task_id':task['id']})
        pipeline.llm.call.assert_not_called()
        self.assertNotIn('text',result)
        self.assertEqual(set(result),{'task_id','raw_path','corrected_path','summary_path'})

    def test_failure_retry_and_no_publish(self):
        self.service.enqueue('rss',self.item());task=self.service.store.claim()
        pipeline=ContentPipeline(self.settings,self.service.store)
        with patch('orchestration.pipeline.extract',side_effect=RuntimeError('secret-key')):
            pipeline.run(task)
        result=self.service.store.task(task['id'])
        self.assertEqual(result['status'],'queued')
        self.assertNotIn('secret-key',result['error'])
        self.assertIsNone(self.service.publish(task['day']))

    def test_correction_failure_uses_raw(self):
        self.service.enqueue('bili',self.item('https://www.bilibili.com/video/BV1Mbp76oESC'))
        task=self.service.store.claim();path=self.settings.content_dir/str(task['id'])/'raw.txt'
        write_text(path,'原始转写')
        pipeline=ContentPipeline(self.settings,self.service.store)
        pipeline.llm.call=Mock(side_effect=RuntimeError('private'))
        result=pipeline.correct({'task_id':task['id'],'raw_path':str(path)})
        self.assertEqual(result['corrected_path'],str(path))
        self.assertIn('纠错失败',self.service.store.task(task['id'])['warning'])

    def test_publish_idempotent_and_preserves_previous(self):
        task=self.done();first=self.service.publish(task['day'])
        self.assertEqual(first['id'],self.service.publish(task['day'])['id'])
        self.service.enqueue('rss',self.item('https://example.com/failure'))
        self.assertEqual(len(self.service.report(task['day'])['entries']),1)
        self.done('https://example.com/next')
        self.service.publish(task['day'])
        # The failed/unprocessed entry never appears in a publication.
        self.assertEqual(len(self.service.report(task['day'])['entries']),2)

    def test_late_update_preserves_final_version(self):
        task=self.done()
        with patch('orchestration.service.is_final',return_value=True):
            first=self.service.publish(task['day'])
            original=Path(first['path']).read_text(encoding='utf-8')
            self.done('https://example.com/late')
            second=self.service.publish(task['day'])
        self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(Path(first['path']).read_text(encoding='utf-8'),original)
        self.assertTrue(self.service.report(task['day'])['supplement'])

    def test_retry_exhaustion_and_manual_retry(self):
        self.service.enqueue('rss',self.item());task=self.service.store.claim()
        self.service.store.execute('UPDATE tasks SET attempts=3 WHERE id=?',(task['id'],))
        pipeline=ContentPipeline(self.settings,self.service.store)
        with patch('orchestration.pipeline.extract',side_effect=RuntimeError('private')):pipeline.run(task)
        self.assertEqual(self.service.store.task(task['id'])['status'],'failed')
        self.service.retry(task['id'])
        self.assertEqual(self.service.store.task(task['id'])['attempts'],0)
        self.assertEqual(self.service.store.claim()['id'],task['id'])

    def test_html_is_escaped_on_public_page(self):
        task=self.done()
        self.service.store.execute('UPDATE tasks SET title=? WHERE id=?',('<script>alert(1)</script>',task['id']))
        self.service.publish(task['day'])
        with TestClient(create_app(self.settings, run_background=False)) as client:
            html=client.get('/').text
        self.assertNotIn('<script>alert(1)</script>',html)
        self.assertIn('&lt;script&gt;',html)

    def test_date_and_final_cutoff(self):
        from datetime import datetime
        from orchestration.service import TZ
        before=datetime(2026,10,19,7,59,tzinfo=TZ).timestamp()
        after=datetime(2026,10,19,8,0,tzinfo=TZ).timestamp()
        self.assertFalse(is_final('2026-10-18',before))
        self.assertTrue(is_final('2026-10-18',after))
        self.assertEqual(day_of(after),'2026-10-19')

    def test_single_video_never_scans_user(self):
        source=Mock();source.get_video_info.return_value={'title':'单视频','pubdate':time.time()}
        with patch.object(self.service,'bili',return_value=source):
            self.service.submit_video('BV1Mbp76oESC')
        source.fetch_page.assert_not_called()
        source.iter_user_videos.assert_not_called()
        self.assertEqual(len(self.service.dashboard()['tasks']),1)

    def test_poll_limit_and_remaining_backlog(self):
        self.service.add_source('bili','123','测试')
        source=Mock();now=time.time()
        source.fetch_page.return_value=([{'bvid':str(i),'created':now,'title':str(i)} for i in range(25)],25)
        with patch.object(self.service,'bili',return_value=source):
            self.service.poll()
            self.assertEqual(len(self.service.dashboard()['tasks']),20)
            self.service.source_action(1,'refresh');self.service.poll()
            self.assertEqual(len(self.service.dashboard()['tasks']),25)

    def test_public_private_separation_and_xss(self):
        self.done();self.service.publish(day_of(time.time()))
        with TestClient(create_app(self.settings, run_background=False)) as client:
            response=client.get('/')
            self.assertEqual(response.status_code,200)
            self.assertEqual(client.get('/admin').status_code,401)
            preview=client.get('/api/public/preview').json()
            self.assertEqual(preview['count'],1)
            self.assertNotIn('raw_path',client.get('/api/public/daily/'+day_of(time.time())).text)
            self.assertEqual(client.get('/data/daily.sqlite').status_code,404)
            self.assertEqual(client.post('/admin/sources',data={'kind':'bili','target':'123','token':'bad'}).status_code,401)

    def test_login_csrf_and_mutations(self):
        import re
        with TestClient(create_app(self.settings, run_background=False)) as client:
            page=client.get('/admin/login')
            token=re.search('name="token" value="([^"]+)"',page.text)[1]
            self.assertEqual(client.post('/admin/login',data={'password':'correct-test-password','token':'bad'}).status_code,403)
            result=client.post('/admin/login',data={'password':'correct-test-password','token':token})
            self.assertEqual(result.status_code,200)
            self.assertIn('让信息按时抵达',result.text)
            token=re.search('name="token" value="([^"]+)"',result.text)[1]
            self.assertEqual(client.post('/admin/sources',data={'kind':'bili','target':'123','name':'test','token':token}).status_code,200)
            self.assertEqual(client.post('/admin/rebuild',data={'day':'invalid','token':token}).status_code,400)

    def test_summary_contract(self):
        for payload in ('{}','{"topic":"技术","bullets":[]}', '{"topic":"脚本","bullets":["x"]}'):
            with self.assertRaises(ValueError):ContentPipeline.parse_summary(payload)

if __name__=='__main__':unittest.main()
