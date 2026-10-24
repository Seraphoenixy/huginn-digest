"""任务列表分页与事件表增长上限。"""
import tempfile
import time
import unittest
from pathlib import Path

from config import Settings
from integration.store import Store
from orchestration.service import DailyService


class TaskPaginationTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='task-page-secret-at-least-32-chars',
                               admin_hash='offline-hash',secure_cookie=False)
        self.service=DailyService(self.settings)
        self.store=self.service.store

    def seed(self,count,status='done',tag=''):
        """URL 唯一；批量入队一次事务写完，避免灌数据比断言还慢。"""
        now=time.time()
        self.store.enqueue_many([{'kind':'rss','url':f'https://example.com/{tag}{i}',
                                  'title':f'任务{tag}{i}','published':now,
                                  'day':'2026-10-24','source_id':None} for i in range(count)])
        self.store.execute('UPDATE tasks SET status=?,updated=? WHERE url LIKE ?',
                           (status,now,f'https://example.com/{tag}%'))

    def test_task_list_is_paginated_instead_of_silently_truncated(self):
        """回归：固定 LIMIT 100 会让第 101 条以后静默消失，界面却写着“全部任务”。"""
        self.seed(120)
        self.assertEqual(self.store.count_tasks('all'),120)
        first=[row['id'] for row in self.store.dashboard_tasks('all',limit=50,offset=0)]
        second=[row['id'] for row in self.store.dashboard_tasks('all',limit=50,offset=50)]
        third=[row['id'] for row in self.store.dashboard_tasks('all',limit=50,offset=100)]
        self.assertEqual(len(first),50)
        self.assertEqual(len(third),20,'最后一页只剩余数')
        self.assertEqual(len(set(first)&set(second)),0,'相邻页不重复')
        self.assertEqual(len(set(first+second+third)),120,'三页合起来覆盖全部任务')

    def test_page_count_and_clamping(self):
        self.seed(120)
        data=self.service.dashboard('all',None,1,1)
        self.assertEqual(data['tasks_total'],120)
        self.assertEqual(data['tasks_pages'],3)
        self.assertEqual(data['tasks_page'],1)
        self.assertEqual(len(data['tasks']),50)
        high=self.service.dashboard('all',None,1,99)
        self.assertEqual(high['tasks_page'],3,'越界页收敛到最后一页')
        bogus=self.service.dashboard('all',None,1,'abc')
        self.assertEqual(bogus['tasks_page'],1)

    def test_count_matches_view_filters(self):
        self.seed(5,'done',tag='d')
        self.seed(3,'failed',tag='f')
        self.assertEqual(self.store.count_tasks('done'),5)
        self.assertEqual(self.store.count_tasks('failed'),3)
        self.assertEqual(self.store.count_tasks('all'),8)


class EventGrowthTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='event-growth-secret-at-least-32',
                               admin_hash='offline-hash',secure_cookie=False)
        self.service=DailyService(self.settings)
        self.store=self.service.store

    def seed_events(self,count,age_days=0):
        stamp=time.time()-age_days*86400
        # 一次事务写入：逐条 event() 会为每条开一次连接。
        with self.store.connect() as db:
            db.executemany('INSERT INTO events(task_id,stage,status,detail,created) VALUES(?,?,?,?,?)',
                           [(i+1,'acquire','ok','',stamp) for i in range(count)])

    def test_old_events_are_pruned(self):
        self.seed_events(10,age_days=60)
        self.store.prune_events(time.time()-30*86400)
        self.assertEqual(self.store.count_events(),0)

    def test_row_cap_bounds_growth_even_within_retention_window(self):
        """只按天数删不够：活跃站点30天仍能堆到上万行。"""
        self.seed_events(300)
        self.store.prune_events(time.time()-30*86400,keep_rows=100)
        self.assertEqual(self.store.count_events(),100,'超出上限的旧记录被删除')
        # 保留的是最新的一批。
        self.assertEqual(self.store.recent_events(1)[0]['id'],300)

    def test_row_cap_keeps_the_newest_events(self):
        self.seed_events(20)
        self.store.prune_events(time.time()-30*86400,keep_rows=5)
        ids=[row['id'] for row in self.store.recent_events(10)]
        self.assertEqual(sorted(ids),[16,17,18,19,20])

    def test_event_pruning_survives_file_cleanup_failure(self):
        """回归：prune_events 曾在 cleanup() 之后，文件清理一抛错就整段被跳过。"""
        from unittest.mock import patch
        self.seed_events(10,age_days=60)
        with patch('integration.cleanup.cleanup',side_effect=RuntimeError('磁盘异常')):
            self.service.run_cleanup()
        self.assertEqual(self.store.count_events(),0,'文件清理失败也要整理事件记录')
        # 失败要如实上报，不能因为事件清理成功就显示成功。
        self.assertTrue(self.service.storage_state()['last_error'])

    def test_cleanup_marks_success_when_nothing_fails(self):
        self.service.run_cleanup()
        state=self.service.storage_state()
        self.assertEqual(state['last_error'],'')
        self.assertGreater(state['last_cleanup'],0)


if __name__=='__main__':unittest.main()
