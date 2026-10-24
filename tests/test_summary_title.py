"""摘要标题：由总结模型随要点一起生成，发布时优先于来源标题。"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient
from config import Settings
from integration.files import write_text
from interaction.web import create_app
from orchestration.pipeline import ContentPipeline
from orchestration.service import DailyService, day_of


class SummaryTitleParseTests(unittest.TestCase):
    def test_title_is_kept_and_stripped(self):
        parsed=ContentPipeline.parse_summary(json.dumps({'topic':'AI 与大模型','title':'  新模型发布  ','bullets':['要点']}))
        self.assertEqual(len(parsed),1,'旧单条结构应包成单元素数组')
        self.assertEqual(parsed[0]['title'],'新模型发布')

    def test_fresh_model_output_requires_a_title(self):
        with self.assertRaises(ValueError):
            ContentPipeline.parse_summary(json.dumps({'topic':'AI 与大模型','bullets':['要点']}),require_title=True)

    def test_title_rejects_blank_multiline_and_overlong(self):
        for title in ('   ','两行\n标题','长'*41):
            with self.subTest(title=title):
                with self.assertRaises(ValueError):
                    ContentPipeline.parse_summary(json.dumps({'topic':'AI 与大模型','title':title,'bullets':['要点']}),
                                                  require_title=True)

    def test_legacy_summary_without_title_still_validates(self):
        """复核磁盘产物用的是宽松校验，否则历史摘要会被判损坏并触发重算。"""
        parsed=ContentPipeline.parse_summary(json.dumps({'topic':'AI 与大模型','bullets':['要点']}))
        self.assertNotIn('title',parsed[0])

    def test_legacy_invalid_title_is_dropped_when_not_required(self):
        parsed=ContentPipeline.parse_summary(json.dumps({'topic':'AI 与大模型','title':'两行\n标题','bullets':['要点']}))
        self.assertNotIn('title',parsed[0])

    def test_prompt_actually_asks_for_a_title(self):
        """提示词与解析器必须同时要求标题，否则模型不会输出、任务只会反复失败。"""
        with tempfile.TemporaryDirectory() as directory:
            pipeline=ContentPipeline(Settings(data_dir=Path(directory),session_secret='summary-title-secret-32-chars',
                                              admin_hash='offline-hash',secure_cookie=False),Mock())
            prompt=pipeline.prompt('summarize')
        self.assertIn('"title"',prompt)
        self.assertIn('标题',prompt)


class SummaryTitlePublishTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='summary-title-secret-32-chars',
                               admin_hash='offline-hash',secure_cookie=False)
        self.service=DailyService(self.settings)

    def add(self,source_title,summary,url='https://example.com/a'):
        self.service.enqueue('rss',{'url':url,'title':source_title,'published':time.time()})
        task=self.service.store.query('SELECT * FROM tasks WHERE url=?',(url,))[0]
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'summary.json',json.dumps(summary,ensure_ascii=False))
        self.service.store.update(task['id'],status='done',summary_path=str(folder/'summary.json'))
        return task

    def publish(self,task):
        day=task['day']
        self.service.publish(day)
        return self.service.report(day)['entries'][0]

    def test_model_title_replaces_source_title(self):
        task=self.add('《震惊！这个视频你一定要看》',{'topic':'AI 与大模型','title':'某团队发布开源推理模型','bullets':['要点']})
        entry=self.publish(task)
        self.assertEqual(entry['title'],'某团队发布开源推理模型')

    def test_legacy_summary_keeps_source_title(self):
        task=self.add('原始标题',{'topic':'AI 与大模型','bullets':['要点']})
        entry=self.publish(task)
        self.assertEqual(entry['title'],'原始标题')

    def test_blank_model_title_falls_back_to_source_title(self):
        task=self.add('原始标题',{'topic':'AI 与大模型','title':'   ','bullets':['要点']})
        entry=self.publish(task)
        self.assertEqual(entry['title'],'原始标题')

    def test_changed_title_publishes_a_new_version(self):
        task=self.add('原始标题',{'topic':'AI 与大模型','title':'第一版标题','bullets':['要点']})
        self.publish(task)
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'summary.json',json.dumps({'topic':'AI 与大模型','title':'第二版标题','bullets':['要点']},ensure_ascii=False))
        self.service.publish(task['day'])
        self.assertEqual(self.service.report(task['day'])['entries'][0]['title'],'第二版标题')
        self.assertEqual(len(self.service.store.query('SELECT id FROM versions WHERE day=?',(task['day'],))),2)

    def test_page_renders_the_model_title(self):
        task=self.add('《必看！》',{'topic':'AI 与大模型','title':'模型写出的标题','bullets':['要点']})
        self.service.publish(task['day'])
        with TestClient(create_app(self.settings,run_background=False)) as client:
            page=client.get(f'/daily/{task["day"]}').text
        self.assertIn('<h3>模型写出的标题</h3>',page)
        self.assertNotIn('《必看！》',page)

    def test_repair_does_not_delete_legacy_summary(self):
        task=self.add('原始标题',{'topic':'AI 与大模型','bullets':['要点']})
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'raw.txt','正文内容')
        pipeline=self.service._ensure_pipeline()
        pipeline.repair_needed(task)
        self.assertTrue((folder/'summary.json').is_file())

    def test_repair_keeps_summary_with_title(self):
        task=self.add('原始标题',{'topic':'AI 与大模型','title':'模型标题','bullets':['要点']})
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'raw.txt','正文内容')
        pipeline=self.service._ensure_pipeline()
        pipeline.repair_needed(task)
        self.assertTrue((folder/'summary.json').is_file())

    def test_repair_still_removes_corrupt_summary(self):
        task=self.add('原始标题',{'topic':'AI 与大模型','bullets':[]})
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'raw.txt','正文内容')
        pipeline=self.service._ensure_pipeline()
        self.assertTrue(pipeline.repair_needed(task))
        self.assertFalse((folder/'summary.json').exists())


if __name__=='__main__':unittest.main()
