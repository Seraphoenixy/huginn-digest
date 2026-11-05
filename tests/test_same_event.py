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

    def add_for_source(self,source_id,title,events,url):
        """在指定订阅来源下追加一个任务：模拟同一来源先后发布的两支视频。"""
        self.service.enqueue('bili',{'url':url,'title':title,'published':time.time()},source_id=source_id)
        task=self.service.store.query('SELECT * FROM tasks WHERE url=?',(url,))[0]
        self.service.store.execute('UPDATE tasks SET day=? WHERE id=?',(self.day,task['id']))
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'summary.json',json.dumps({'events':events},ensure_ascii=False))
        self.service.store.update(task['id'],status='done',summary_path=str(folder/'summary.json'))

    def shared_source(self,name):
        self.service.store.execute('INSERT INTO sources(kind,target,name,created) VALUES("bili",?,?,?)',
                                   (name,name,time.time()))
        return self.service.store.query('SELECT id FROM sources WHERE name=?',(name,))[0]['id']

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
    def test_brief_and_detail_videos_of_same_source_group_by_head(self):
        """实测：同一来源先发简报再发详情，同一项目的 subject 措辞不同但前导专名相同。

        「HowToLiveBetter 提供开源人生攻略」与「HowToLiveBetter 开源生活建议筛选指南」
        互不包含，包含匹配认不出来；前导专名配对负责把这样的两支视频归到一组。
        """
        source=self.shared_source('Oraink灵砚【Github项目】')
        self.add_for_source(source,'简报视频',
                            [{'topic':'其他','title':'HowToLiveBetter 开源人生攻略','bullets':['简报版'],
                              'subject':'HowToLiveBetter 提供开源人生攻略'}],
                            'https://www.bilibili.com/video/BV1brief11')
        self.add_for_source(source,'详情视频',
                            [{'topic':'其他','title':'HowToLiveBetter 开源生活指南','bullets':['详情版'],
                              'subject':'HowToLiveBetter 开源生活建议筛选指南'}],
                            'https://www.bilibili.com/video/BV1detail11')
        result=self.repeats()
        self.assertEqual(result.get('HowToLiveBetter 开源人生攻略'),2)
        self.assertEqual(result.get('HowToLiveBetter 开源生活指南'),2)
        page=self.client.get(f'/daily/{self.day}').text
        self.assertEqual(page.count('class="same-event"'),1,'简报与详情收进同一个块')

    def test_standalone_tasks_with_same_head_do_not_group(self):
        """独立提交没有 source_id：两条各自是一个来源，前导专名相同也不配对。"""
        self.add('a',[{'topic':'其他','title':'OneLife 开源人生攻略','bullets':['a'],
                       'subject':'OneLife 提供开源人生攻略'}],'https://example.com/one-a')
        self.add('b',[{'topic':'其他','title':'OneLife 开源生活指南','bullets':['b'],
                       'subject':'OneLife 开源生活建议筛选指南'}],'https://example.com/one-b')
        result=self.repeats()
        self.assertIsNone(result.get('OneLife 开源人生攻略'))
        self.assertIsNone(result.get('OneLife 开源生活指南'))

    def test_head_in_three_videos_does_not_group(self):
        """同一专名当日出现三次：无法确认哪两条是同一事件，全部不配对。"""
        source=self.shared_source('三视频来源')
        for suffix,url in (('a','https://www.bilibili.com/video/BV1x11111'),
                           ('b','https://www.bilibili.com/video/BV1x22222'),
                           ('c','https://www.bilibili.com/video/BV1x33333')):
            self.add_for_source(source,f'视频{suffix}',
                                [{'topic':'AI 与大模型','title':f'AlphaLab 用法{suffix}','bullets':[suffix],
                                  'subject':f'AlphaLab 提供{suffix}用法'}],url)
        result=self.repeats()
        for suffix in ('a','b','c'):
            self.assertIsNone(result.get(f'AlphaLab 用法{suffix}'))

    def test_short_heads_do_not_cause_false_matches(self):
        """过短专名（fal、kimi 一类）区分度不足，不参与前导专名配对。"""
        source=self.shared_source('短专名来源')
        self.add_for_source(source,'视频a',
                            [{'topic':'行业与公司','title':'Fal 完成融资','bullets':['a'],
                              'subject':'Fal 完成 16.5 亿美元融资'}],
                            'https://www.bilibili.com/video/BV1short1')
        self.add_for_source(source,'视频b',
                            [{'topic':'行业与公司','title':'Fal 推出新品','bullets':['b'],
                              'subject':'Fal 发布新服务'}],
                            'https://www.bilibili.com/video/BV1short2')
        result=self.repeats()
        self.assertIsNone(result.get('Fal 完成融资'))
        self.assertIsNone(result.get('Fal 推出新品'))


if __name__=='__main__':unittest.main()
