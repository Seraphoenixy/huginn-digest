"""同事件标注：不同来源的同一件事要能识别，但不能大面积误判。"""
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


class SameEventTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='same-event-secret-at-least-32-chars',
                               admin_hash=hash_password('abcdefghij'),secure_cookie=False)
        self.service=DailyService(self.settings)
        self.app=create_app(self.settings,run_background=False)
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)
        self.day='2026-10-24'

    def add(self,title,events,url):
        self.service.enqueue('rss',{'url':url,'title':title,'published':time.time()})
        task=self.service.store.query('SELECT * FROM tasks WHERE url=?',(url,))[0]
        self.service.store.execute('UPDATE tasks SET day=? WHERE id=?',(self.day,task['id']))
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'summary.json',json.dumps({'events':events},ensure_ascii=False))
        self.service.store.update(task['id'],status='done',summary_path=str(folder/'summary.json'))

    def repeats(self):
        self.service.publish(self.day)
        model=self.service.briefing(self.day)
        return {entry['title']:entry.get('repeat') for section in model['sections']
                for entry in section['entries']}

    def test_subject_and_subject_plus_action_are_the_same_event(self):
        """实测：B站写「Kimi K3.1 Pro 发布」，早报写「Kimi K3.1 Pro」，是同一件事。"""
        self.add('视频',[{'topic':'AI 与大模型','title':'Kimi K3.1 Pro 正式发布',
                          'bullets':['视频版'],'subject':'Kimi K3.1 Pro 发布'}],
                 'https://example.com/video')
        self.add('早报',[{'topic':'AI 与大模型','title':'月之暗面发布 Kimi K3.1 Pro',
                          'bullets':['文字版'],'subject':'Kimi K3.1 Pro'}],
                 'https://example.com/daily')
        result=self.repeats()
        self.assertEqual(result.get('Kimi K3.1 Pro 正式发布'),2)
        self.assertEqual(result.get('月之暗面发布 Kimi K3.1 Pro'),2)

    def test_short_subjects_do_not_cause_false_matches(self):
        """两字主体参与包含匹配会把无关条目全判成同题。"""
        self.add('a',[{'topic':'游戏','title':'游戏新闻','bullets':['a'],'subject':'游戏'}],
                 'https://example.com/a')
        self.add('b',[{'topic':'游戏','title':'游戏引擎更新','bullets':['b'],'subject':'游戏引擎更新'}],
                 'https://example.com/b')
        result=self.repeats()
        self.assertIsNone(result.get('游戏新闻'),'过短主体不应被判为同题')
        self.assertIsNone(result.get('游戏引擎更新'))

    def test_shared_prefix_but_different_events_are_not_matched(self):
        self.add('a',[{'topic':'行业与公司','title':'OpenAI 融资估值下调','bullets':['a'],'subject':'OpenAI 融资'}],
                 'https://example.com/a')
        self.add('b',[{'topic':'AI 与大模型','title':'OpenAI 发布新模型','bullets':['b'],'subject':'OpenAI 发布新模型'}],
                 'https://example.com/b')
        result=self.repeats()
        self.assertIsNone(result.get('OpenAI 融资估值下调'))
        self.assertIsNone(result.get('OpenAI 发布新模型'))

    def test_different_events_of_same_company_are_not_matched(self):
        """同一家公司的两件不同事：登月计划与模型发布，不能算同题。"""
        self.add('视频',[{'topic':'AI 与大模型','title':'Kimi K3.1 Pro 正式发布','bullets':['a'],'subject':'Kimi K3.1 Pro 发布'},
                         {'topic':'行业与公司','title':'Kimi 推出登月同行计划','bullets':['b'],'subject':'Kimi 登月同行计划'}],
                 'https://example.com/video')
        result=self.repeats()
        self.assertIsNone(result.get('Kimi K3.1 Pro 正式发布'),'只有一条来源，不该有同题')
        self.assertIsNone(result.get('Kimi 推出登月同行计划'))

    def test_page_shows_badge_only_when_repeated(self):
        self.add('视频',[{'topic':'AI 与大模型','title':'Kimi K3.1 Pro 正式发布',
                          'bullets':['视频版'],'subject':'Kimi K3.1 Pro 发布'}],
                 'https://example.com/video')
        self.add('早报',[{'topic':'AI 与大模型','title':'月之暗面发布 Kimi K3.1 Pro',
                          'bullets':['文字版'],'subject':'Kimi K3.1 Pro'}],
                 'https://example.com/daily')
        self.add('另一个',[{'topic':'游戏','title':'新作公布','bullets':['c'],'subject':'新作公布时间'}],
                 'https://example.com/other')
        self.service.publish(self.day)
        page=self.client.get(f'/daily/{self.day}').text
        self.assertEqual(page.count('class="same-event"'),1,'只有同题那一组被框起来')
        self.assertIn('同一事件 · 2 个来源',page)
        self.assertIn('视频版',page)
        self.assertIn('文字版',page)
        self.assertIn('新作公布',page)


if __name__=='__main__':unittest.main()
