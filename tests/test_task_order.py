"""任务视图排序：全部任务按最近更新，其余视图按状态分组。"""
import tempfile
import time
import unittest
from pathlib import Path

from config import Settings
from integration.store import Store


class TaskOrderingTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='task-order-secret-at-least-32-chars',
                               admin_hash='offline-hash',secure_cookie=False)
        self.store=Store(self.settings.db_path)

    def seed(self,title,status,updated):
        self.store.enqueue({'kind':'rss','url':f'https://example.com/{title}','title':title,
                            'published':time.time(),'day':'2026-10-24','source_id':None})
        row=self.store.query('SELECT id FROM tasks WHERE title=?',(title,))[0]
        self.store.execute('UPDATE tasks SET status=?,updated=? WHERE id=?',(status,updated,row['id']))
        return row['id']

    def test_all_view_is_ordered_by_recency(self):
        """回归：全部任务曾按状态分组，刚完成的任务被挤到取消任务之后。"""
        old_done=self.seed('旧的完成','done',1000)
        canceled=self.seed('取消的','canceled',5000)
        fresh_done=self.seed('新的完成','done',9000)
        ids=[row['id'] for row in self.store.dashboard_tasks('all')]
        self.assertEqual(ids,[fresh_done,canceled,old_done],'全部任务应按更新时间倒序')

    def test_other_views_keep_status_grouping(self):
        running=self.seed('进行中','running',1000)
        failed=self.seed('失败','failed',9000)
        done=self.seed('完成','done',9500)
        ids=[row['id'] for row in self.store.dashboard_tasks('failed')]
        self.assertEqual(ids,[failed])
        ids=[row['id'] for row in self.store.dashboard_tasks('done')]
        self.assertEqual(ids,[done])
        self.assertNotIn(running,ids)

    def test_running_tasks_still_sort_first_within_grouped_views(self):
        old_running=self.seed('早开始的进行中','running',100)
        queued=self.seed('等待中','queued',9000)
        ids=[row['id'] for row in self.store.dashboard_tasks('active')]
        self.assertEqual(ids,[old_running,queued],'进行中仍排在等待之前，即使更新时间更早')


if __name__=='__main__':unittest.main()
