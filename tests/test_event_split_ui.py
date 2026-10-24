"""事件级拆分的界面表现：同题提示与管理页派生条数。"""
import json
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from config import Settings
from integration.auth import hash_password
from integration.files import write_text
from interaction.web import create_app
from orchestration.service import DailyService


class EventSplitUiTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='split-ui-secret-at-least-32-chars',
                               admin_hash=hash_password('abcdefghij'),secure_cookie=False)
        self.service=DailyService(self.settings)
        self.app=create_app(self.settings,run_background=False)
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)
        self.day='2026-10-24'

    def add(self,title,events,url=None):
        url=url or f'https://example.com/{title}'
        self.service.enqueue('rss',{'url':url,'title':title,'published':time.time()})
        task=self.service.store.query('SELECT * FROM tasks WHERE url=?',(url,))[0]
        self.service.store.execute('UPDATE tasks SET day=? WHERE id=?',(self.day,task['id']))
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'summary.json',json.dumps({'events':events},ensure_ascii=False))
        self.service.store.update(task['id'],status='done',summary_path=str(folder/'summary.json'))
        return task['id']

    def test_duplicate_subject_is_flagged_without_merging(self):
        self.add('B站视频',[{'topic':'AI 与大模型','title':'Kimi K3.1 Pro 发布',
                            'bullets':['视频版'],'subject':'Kimi K3.1 Pro'}])
        self.add('早报',[{'topic':'AI 与大模型','title':'月之暗面发布 Kimi',
                         'bullets':['文字版'],'subject':'kimi k3.1 pro '}])
        self.service.publish(self.day)
        page=self.client.get(f'/daily/{self.day}').text
        # 同题条目收进同一个分组容器，标签写明来源数。
        self.assertIn('class="same-event"',page,'同题应被框在同一组里')
        self.assertIn('同一事件 · 2 个来源',page)
        # 不合并：两条内容都还在。
        self.assertIn('视频版',page)
        self.assertIn('文字版',page)

    def test_single_source_has_no_repeat_badge(self):
        self.add('单条',[{'topic':'游戏','title':'新作公布','bullets':['只有一个来源'],'subject':'新作'}])
        self.service.publish(self.day)
        page=self.client.get(f'/daily/{self.day}').text
        self.assertNotIn('class="same-event"',page,'单条事件不该出现分组容器')

    def test_admin_shows_derived_entry_count(self):
        task=self.add('汇总',[
            {'topic':'AI 与大模型','title':'第一件','bullets':['a']},
            {'topic':'游戏','title':'第二件','bullets':['b']},
            {'topic':'时事','title':'第三件','bullets':['c']},
        ])
        self.service.publish(self.day)
        self.client.cookies.set('daily_session',self.service.new_session(True))
        page=self.client.get('/admin').text
        self.assertIn('已拆为 3 条日报',page)

    def test_single_event_task_does_not_show_count(self):
        self.add('单条',[{'topic':'游戏','title':'新作公布','bullets':['a']}])
        self.service.publish(self.day)
        self.client.cookies.set('daily_session',self.service.new_session(True))
        page=self.client.get('/admin').text
        self.assertNotIn('已拆为 1 条日报',page,'单事件任务不需要这行噪音')


if __name__=='__main__':unittest.main()
