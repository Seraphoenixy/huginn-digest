"""事件级拆分：一条汇总来源拆成多条日报条目，链接走材料白名单。"""
import json
import re
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient
from config import Settings
from integration.auth import hash_password
from integration.files import write_text
from interaction.web import create_app
from orchestration.pipeline import ContentPipeline
from orchestration.service import DailyService


def events(*items):
    return json.dumps([dict(item) for item in items],ensure_ascii=False)


class ParseEventsTests(unittest.TestCase):
    def test_single_event_material_still_returns_one_element(self):
        parsed=ContentPipeline.parse_summary(events(
            {'topic':'AI 与大模型','title':'模型发布','bullets':['要点']}))
        self.assertEqual(len(parsed),1)
        self.assertEqual(parsed[0]['title'],'模型发布')

    def test_legacy_dict_payload_is_wrapped(self):
        """旧摘要与新分片都是单条 dict，必须继续被接受，否则历史产物会被判损坏。"""
        parsed=ContentPipeline.parse_summary(json.dumps(
            {'topic':'AI 与大模型','title':'旧标题','bullets':['旧要点']},ensure_ascii=False))
        self.assertEqual(len(parsed),1)
        self.assertEqual(parsed[0]['topic'],'AI 与大模型')

    def test_multiple_events_keep_their_own_topic_and_title(self):
        parsed=ContentPipeline.parse_summary(events(
            {'topic':'AI 与大模型','title':'模型发布','bullets':['a']},
            {'topic':'游戏','title':'新作公布','bullets':['b']},
            {'topic':'时事','title':'政策调整','bullets':['c']}))
        self.assertEqual([item['topic'] for item in parsed],['AI 与大模型','游戏','时事'])
        self.assertEqual([item['title'] for item in parsed],['模型发布','新作公布','政策调整'])

    def test_unknown_topic_in_any_event_rejects_the_whole_summary(self):
        with self.assertRaises(ValueError):
            ContentPipeline.parse_summary(events(
                {'topic':'AI 与大模型','title':'正常','bullets':['a']},
                {'topic':'玄学','title':'越界','bullets':['b']}))

    def test_url_must_appear_in_the_material(self):
        real='https://www.kimi.ai/blog/kimi-k3-1-pro'
        text=events({'topic':'AI 与大模型','title':'模型发布','bullets':['a'],
                     'url':real},
                    {'topic':'AI 与大模型','title':'编造链接','bullets':['b'],
                     'url':'https://example.com/made-up'})
        parsed=ContentPipeline.parse_summary(text,allowed_urls={real})
        self.assertEqual(parsed[0]['url'],real)
        self.assertNotIn('url',parsed[1],'白名单外的链接必须被丢弃')

    def test_subject_is_kept_for_duplicate_detection(self):
        parsed=ContentPipeline.parse_summary(events(
            {'topic':'AI 与大模型','title':'模型发布','bullets':['a'],'subject':'Kimi K3.1 Pro 发布'}))
        self.assertEqual(parsed[0]['subject'],'Kimi K3.1 Pro 发布')

    def test_partials_merge_by_subject(self):
        merged=ContentPipeline.merge_partials([
            {'topic':'AI 与大模型','title':'模型发布','bullets':['a'],'subject':'Kimi 发布'},
            {'topic':'AI 与大模型','title':'模型发布','bullets':['b'],'subject':'Kimi 发布'},
            {'topic':'游戏','title':'新作公布','bullets':['c'],'subject':'新作'},
        ])
        self.assertEqual(len(merged),2,'同一 subject 跨片重复应合并')
        self.assertEqual(merged[0]['bullets'],['a','b'])


class PublishSplitTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='split-secret-at-least-32-characters',
                               admin_hash=hash_password('abcdefghij'),secure_cookie=False)
        self.service=DailyService(self.settings)
        self.app=create_app(self.settings,run_background=False)
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)
        self.day='2026-10-24'

    def add_task(self,title,payload,url=None):
        url=url or f'https://example.com/{title}'
        self.service.enqueue('rss',{'url':url,'title':title,'published':time.time()})
        task=self.service.store.query('SELECT * FROM tasks WHERE url=?',(url,))[0]
        self.service.store.execute('UPDATE tasks SET day=? WHERE id=?',(self.day,task['id']))
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'summary.json',payload)
        self.service.store.update(task['id'],status='done',summary_path=str(folder/'summary.json'))
        return task['id']

    def test_one_source_expands_into_many_entries(self):
        """回归：一期早报曾被压成 1 条条目、25 条要点，标题只覆盖第一件事。"""
        task=self.add_task('AI 早报 2026-10-24',events(
            {'topic':'AI 与大模型','title':'Kimi K3.1 Pro 发布','bullets':['三万亿参数']},
            {'topic':'开发与开源','title':'Cursor 新增桌面控制','bullets':['可操作 iOS 模拟器']},
            {'topic':'政策与监管','title':'出口管制目录调整','bullets':['拟增列 23 项']}))
        self.service.publish(self.day)
        report=self.service.report(self.day)
        self.assertEqual(len(report['entries']),3,'一条来源应拆成三条条目')
        self.assertEqual({entry['topic'] for entry in report['entries']},
                         {'AI 与大模型','开发与开源','政策与监管'})
        self.assertEqual([entry['title'] for entry in report['entries']],
                         ['Kimi K3.1 Pro 发布','Cursor 新增桌面控制','出口管制目录调整'])
        # 条目仍能回溯到来源任务，撤回按任务生效。
        self.assertEqual({entry['task_id'] for entry in report['entries']},{task})

    def test_entries_get_distinct_anchors(self):
        self.add_task('汇总',events(
            {'topic':'AI 与大模型','title':'第一件','bullets':['a']},
            {'topic':'游戏','title':'第二件','bullets':['b']}))
        self.service.publish(self.day)
        page=self.client.get(f'/daily/{self.day}').text
        anchors=re.findall(r'<details class="entry" id="entry-([^"]+)"',page)
        self.assertEqual(len(anchors),2)
        self.assertEqual(len(set(anchors)),2,'同源多事件必须有各自锚点')

    def test_event_url_wins_over_source_url(self):
        real='https://www.kimi.ai/blog/kimi-k3-1-pro'
        self.add_task('汇总',events(
            {'topic':'AI 与大模型','title':'带链接的','bullets':['a'],'url':real},
            {'topic':'AI 与大模型','title':'没有链接的','bullets':['b']}))
        self.service.publish(self.day)
        report=self.service.report(self.day)
        self.assertEqual(report['entries'][0]['url'],real)
        self.assertEqual(report['entries'][1]['url'],'https://example.com/汇总','无链接时回退来源主地址')

    def test_retraction_removes_all_entries_of_the_task(self):
        task=self.add_task('汇总',events(
            {'topic':'AI 与大模型','title':'第一件','bullets':['a']},
            {'topic':'游戏','title':'第二件','bullets':['b']}))
        other=self.add_task('单条',events({'topic':'时事','title':'第三件','bullets':['c']}))
        self.service.publish(self.day)
        self.service.retract_entry(task,'来源有误')
        report=self.service.report(self.day)
        self.assertEqual([entry['title'] for entry in report['entries']],['第三件'],'按任务撤回会撤掉其全部事件')
        self.assertEqual(report['entries'][0]['task_id'],other)

    def test_sections_split_by_topic_not_by_source(self):
        self.add_task('汇总',events(
            {'topic':'游戏','title':'游戏新闻','bullets':['a']},
            {'topic':'动漫','title':'动漫新闻','bullets':['b']}))
        self.service.publish(self.day)
        model=self.service.briefing(self.day)
        self.assertEqual([section['topic'] for section in model['sections']],['游戏','动漫'])


if __name__=='__main__':unittest.main()
