"""删除来源后的采集边界：不复活、不抓取、id 不复用。"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Settings
from orchestration.service import DailyService, day_of


def settings_for(directory):
    return Settings(data_dir=Path(directory),
                    session_secret='source-delete-secret-more-than-32-chars',
                    admin_hash='offline-hash', secure_cookie=False)


class SourceDeleteTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service=DailyService(settings_for(self.directory.name))
        self.addCleanup(lambda: patch.stopall())

    def add_rss(self,target,name):
        with patch('orchestration.service.public_url',side_effect=lambda url:url):
            return self.service.add_source('rss',target,name)

    def test_deleted_source_is_not_polled_again(self):
        source=self.add_rss('https://example.com/feed.xml','已删来源')
        self.service.source_action(source,'delete')
        fetched=[]
        with patch('orchestration.service.discover',side_effect=lambda target,now:fetched.append(target) or []):
            self.service.poll()
            self.service.tick()
        self.assertEqual(fetched,[])
        self.assertEqual(self.service.store.sources(),[])

    def test_delete_cancels_queued_tasks_so_body_is_not_fetched(self):
        """回归：删除来源曾只删索引行，其排队任务仍会下载该来源的正文。"""
        source=self.add_rss('https://example.com/feed.xml','已删来源')
        self.service.enqueue('rss',{'url':'https://example.com/old-article','title':'旧文章',
                                    'published':time.time()},source)
        self.service.source_action(source,'delete')
        fetched=[]
        with patch('orchestration.pipeline.extract',
                   side_effect=lambda url,body:fetched.append(url) or ('正文','')):
            self.service.tick()
        self.assertEqual(fetched,[])
        task=self.service.store.query('SELECT * FROM tasks')[0]
        self.assertEqual(task['status'],'canceled')
        self.assertEqual(task['error'],'source: SourceDeleted')

    def test_canceled_task_is_never_claimed_and_not_retryable(self):
        source=self.add_rss('https://example.com/feed.xml','已删来源')
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'旧文章',
                                    'published':time.time()},source)
        self.service.source_action(source,'delete')
        self.assertIsNone(self.service.store.claim())
        task_id=self.service.store.query('SELECT id FROM tasks')[0]['id']
        self.service.retry(task_id)
        self.assertEqual(self.service.store.task(task_id)['status'],'canceled')

    def test_rebuild_does_not_resurrect_deleted_source_tasks(self):
        source=self.add_rss('https://example.com/feed.xml','已删来源')
        day=day_of(time.time())
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'旧文章',
                                    'published':time.time()},source)
        self.service.source_action(source,'delete')
        fetched=[]
        self.service.request_rebuild(day)
        with patch('orchestration.service.discover',side_effect=lambda target,now:fetched.append(target) or []), \
             patch('orchestration.pipeline.extract',side_effect=lambda url,body:fetched.append(url) or ('正文','')):
            self.service.process_rebuild()
        self.assertEqual(fetched,[])
        self.assertEqual(self.service.store.query('SELECT status FROM tasks')[0]['status'],'canceled')

    def test_rebuild_reports_skipped_deleted_source_tasks(self):
        source=self.add_rss('https://example.com/feed.xml','已删来源')
        day=day_of(time.time())
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'旧文章',
                                    'published':time.time()},source)
        self.service.source_action(source,'delete')
        self.service.request_rebuild(day)
        self.service.process_rebuild()
        job=self.service.store.rebuilds()[0]
        self.assertIn('来源已删除',job['error'])

    def test_enqueue_rejects_task_for_deleted_source(self):
        source=self.add_rss('https://example.com/feed.xml','已删来源')
        self.service.source_action(source,'delete')
        inserted=self.service.enqueue('rss',{'url':'https://example.com/race','title':'并发补回',
                                             'published':time.time()},source)
        self.assertFalse(inserted)
        self.assertEqual(self.service.store.query('SELECT id FROM tasks'),[])

    def test_source_ids_are_never_reused(self):
        """回归：删除最大id后SQLite复用该号，旧任务会显示成新来源。"""
        first=self.add_rss('https://example.com/a.xml','旧来源')
        self.service.enqueue('rss',{'url':'https://example.com/old','title':'旧文章',
                                    'published':time.time()},first)
        self.service.source_action(first,'delete')
        second=self.add_rss('https://example.com/b.xml','新来源')
        self.assertEqual(second,first+1)
        task=self.service.store.task(self.service.store.query('SELECT id FROM tasks')[0]['id'])
        self.assertEqual(task['source_id'],first)
        rows=self.service.store.dashboard_tasks('all')
        self.assertEqual(rows[0]['source_name'],'已删除的订阅来源')

    def test_source_id_sequence_seeds_from_existing_database(self):
        """旧库无 source_id_seq 时，种子取 sources 与 tasks 引用的最大值。"""
        with tempfile.TemporaryDirectory() as directory:
            service=DailyService(settings_for(directory))
            with patch('orchestration.service.public_url',side_effect=lambda url:url):
                first=service.add_source('rss','https://example.com/a.xml','a')
            service.store.execute("DELETE FROM meta WHERE key='source_id_seq'")
            service.store.execute('DELETE FROM sources WHERE id=?',(first,))
            service.store.execute("INSERT INTO tasks(source_id,kind,url,title,published,day,updated,status) "
                                  "VALUES(?,?,?,?,?,?,?,'canceled')",
                                  (first,'rss','https://example.com/old','old',time.time(),'2026-01-01',time.time()))
            with patch('orchestration.service.public_url',side_effect=lambda url:url):
                second=service.add_source('rss','https://example.com/b.xml','b')
            self.assertGreater(second,first)

    def test_delete_cancels_failed_tasks_too_so_retry_cannot_revive_them(self):
        """回归：failed 任务若保留，管理页“重试”会把它重新入队，等于绕过删除。"""
        source=self.add_rss('https://example.com/feed.xml','已删来源')
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'失败任务',
                                    'published':time.time()},source)
        task_id=self.service.store.query('SELECT id FROM tasks')[0]['id']
        self.service.store.execute("UPDATE tasks SET status='failed',attempts=3,error='summarize: CloudError' "
                                   'WHERE id=?',(task_id,))
        self.service.source_action(source,'delete')
        self.assertEqual(self.service.store.task(task_id)['status'],'canceled')
        self.service.retry(task_id)
        self.assertEqual(self.service.store.task(task_id)['status'],'canceled')
        self.assertIsNone(self.service.store.claim())

    def test_retry_still_works_when_source_exists(self):
        """取消逻辑不能误伤正常来源的失败重试。"""
        source=self.add_rss('https://example.com/feed.xml','正常来源')
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'失败任务',
                                    'published':time.time()},source)
        task_id=self.service.store.query('SELECT id FROM tasks')[0]['id']
        self.service.store.execute("UPDATE tasks SET status='failed',attempts=3 WHERE id=?",(task_id,))
        self.service.retry(task_id)
        self.assertEqual(self.service.store.task(task_id)['status'],'queued')

    def test_orphan_tasks_from_manual_deletion_are_canceled(self):
        """兜底：直接改库删除来源时，排队任务也不认领。"""
        source=self.add_rss('https://example.com/feed.xml','手工删除')
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'旧文章',
                                    'published':time.time()},source)
        self.service.store.execute('DELETE FROM sources WHERE id=?',(source,))
        self.assertIsNone(self.service.store.claim())
        self.assertEqual(self.service.store.cancel_orphan_tasks(),1)
        self.assertEqual(self.service.store.query('SELECT status FROM tasks')[0]['status'],'canceled')

    def test_recovery_cancels_running_task_of_deleted_source(self):
        source=self.add_rss('https://example.com/feed.xml','崩溃期删除')
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'旧文章',
                                    'published':time.time()},source)
        self.service.store.execute("UPDATE tasks SET status='running'")
        self.service.store.execute('DELETE FROM sources WHERE id=?',(source,))
        self.service.store.recover()
        self.assertEqual(self.service.store.query('SELECT status FROM tasks')[0]['status'],'canceled')

    def test_delete_between_claim_and_acquire_stops_without_fetching(self):
        """回归：认领后、抓取前来源被删除，不能继续下载正文，也不进入重试。"""
        source=self.add_rss('https://example.com/feed.xml','间隙删除')
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'竞争任务',
                                    'published':time.time()},source)
        task=self.service.store.claim()
        self.service.store.execute('DELETE FROM sources WHERE id=?',(source,))
        # 直接改库删除时任务仍是 running；acquire 必须自行发现来源已不存在。
        self.assertEqual(self.service.store.task(task['id'])['status'],'running')
        fetched=[]
        pipeline=self.service._ensure_pipeline()
        with patch('orchestration.pipeline.extract',
                   side_effect=lambda url,body:fetched.append(url) or ('正文','')):
            pipeline.run(task)
        self.assertEqual(fetched,[])
        stored=self.service.store.task(task['id'])
        self.assertEqual(stored['status'],'canceled')
        self.assertEqual(stored['error'],'source: SourceDeleted')
        # 取消不占用重试次数，也不会被再次认领。
        self.assertIsNone(self.service.store.claim())

    def test_tick_cleans_orphan_tasks_and_reports_to_dashboard(self):
        source=self.add_rss('https://example.com/feed.xml','手工删除')
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'孤儿任务',
                                    'published':time.time()},source)
        self.service.store.execute('DELETE FROM sources WHERE id=?',(source,))
        self.assertEqual(self.service.store.task_counts()['queued'],1)
        self.service.tick()
        self.assertEqual(self.service.store.query('SELECT status FROM tasks')[0]['status'],'canceled')
        self.assertEqual(self.service.store.task_counts()['queued'],0)

    def test_dashboard_shows_canceled_view(self):
        source=self.add_rss('https://example.com/feed.xml','已删来源')
        self.service.enqueue('rss',{'url':'https://example.com/a','title':'取消的任务',
                                    'published':time.time()},source)
        self.service.source_action(source,'delete')
        data=self.service.dashboard('canceled')
        self.assertEqual(len(data['tasks']),1)
        self.assertEqual(data['tasks'][0]['state_label'],'已取消')
        self.assertEqual(data['tasks'][0]['error_text'],'来源已删除，任务已取消')
        self.assertEqual(data['task_counts']['canceled'],1)
        self.assertEqual(self.service.dashboard('active')['tasks'],[])


if __name__=='__main__':unittest.main()
