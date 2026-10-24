import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from config import Settings
from integration.files import write_text
from orchestration.pipeline import ContentPipeline, split_text
from orchestration.service import DailyService


class SummaryCoverageTests(unittest.TestCase):
    def test_sentence_boundaries_preserve_all_characters(self):
        text='第一条消息。第二条消息。\n第三条消息很长很长很长。最后消息！'
        chunks=split_text(text,12)
        self.assertEqual(''.join(chunks),text)
        self.assertTrue(all(0<len(chunk)<=12 for chunk in chunks))
        self.assertEqual(chunks[0],'第一条消息。第二条消息。')
        self.assertEqual(split_text('甲'*25,10),['甲'*10,'甲'*10,'甲'*5])
        self.assertEqual(split_text('',10),[])
        with self.assertRaises(ValueError):split_text('正文',0)

    def test_more_than_eight_independent_topics_are_accepted(self):
        result={'topic':'行业','bullets':[f'独立新闻{i}' for i in range(12)]}
        self.assertEqual(ContentPipeline.parse_summary(json.dumps(result)),result)
        result['bullets']=['消息']*65
        with self.assertRaises(ValueError):ContentPipeline.parse_summary(json.dumps(result))

    def test_old_cache_archived_and_unchanged_result_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(data_dir=Path(directory))
            service=DailyService(settings)
            service.enqueue('rss',{'url':'https://example.com/news','title':'news','published':1})
            task=service.store.claim()
            pipeline=ContentPipeline(settings,service.store)
            state={'task_id':task['id']}
            folder=pipeline.folder(state)
            raw=folder/'raw.txt'
            write_text(raw,'首条新闻。末条新闻。')
            state['corrected_path']=str(raw)
            old=json.dumps({'topic':'行业','bullets':['旧摘要']})
            write_text(folder/'summary.json',old)
            write_text(folder/'summary_part_0.json',old)
            pipeline.llm.call=Mock(return_value=json.dumps({'topic':'行业','title':'合并后标题','bullets':['首条新闻','末条新闻']}))
            pipeline.summarize(state)
            pipeline.llm.call.assert_called_once()
            archive=list(folder.glob('previous_summary_*'))
            self.assertEqual(len(archive),1)
            self.assertEqual((archive[0]/'summary.json').read_text(encoding='utf-8'),old)
            pipeline.llm.call.reset_mock()
            pipeline.summarize(state)
            pipeline.llm.call.assert_not_called()
            self.assertFalse(pipeline.repair_needed(task))
            write_text(raw,'改变后的原稿。')
            self.assertTrue(pipeline.repair_needed(task))
            pipeline.summarize(state)
            pipeline.llm.call.assert_called_once()
            pipeline.prompt=Mock(return_value='修改后的摘要要求')
            self.assertTrue(pipeline.repair_needed(task))
