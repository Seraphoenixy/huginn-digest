import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from config import ROOT,Settings
from integration.files import read_text,write_text
from orchestration.pipeline import ContentPipeline
from orchestration.service import DailyService

class SummaryEventIdentityTests(unittest.TestCase):
    def test_later_chunk_receives_same_task_identities_including_after_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'config.toml'
            config.write_text((ROOT/'config.example.toml').read_text(encoding='utf-8'),encoding='utf-8')
            settings=Settings(config_file=config)
            settings.values['llm']['chunk_chars']=1000
            service=DailyService(settings)
            service.enqueue('rss',{'url':'https://example.com/current','title':'当前来源','published':1})
            task=service.store.claim()
            pipeline=ContentPipeline(settings,service.store)
            state={'task_id':task['id']}
            folder=pipeline.folder(state)
            raw=folder/'raw.txt'
            write_text(raw,'甲'*999+'。'+'乙'*100+'。')
            state['corrected_path']=str(raw)
            identity='Artificial Analysis 发布指数 v5'
            def event(title,bullet):
                return json.dumps({'events':[dict(topic='研究',title=title,subject=identity,bullets=[bullet])]},ensure_ascii=False)
            pipeline.llm.call=Mock(side_effect=[event('智能指数 v5 发布','前段独有事实'),RuntimeError('offline')])
            with self.assertRaises(RuntimeError):pipeline.summarize(state)
            self.assertNotIn('本任务前段事件索引',pipeline.llm.call.call_args_list[0].args[1])
            request=pipeline.llm.call.call_args_list[1].args[1]
            self.assertIn(identity,request)
            self.assertNotIn('前段独有事实',request)
            pipeline.llm.call=Mock(return_value=event('v5 指数指标说明','后段新增指标'))
            result=pipeline.summarize(state)
            pipeline.llm.call.assert_called_once()
            self.assertIn(identity,pipeline.llm.call.call_args.args[1])
            events=json.loads(read_text(result['summary_path']))['events']
            self.assertEqual(len(events),1)
            self.assertEqual(events[0]['bullets'],['前段独有事实','后段新增指标'])

    def test_prompt_defines_event_before_title_and_preserves_distinct_events(self):
        text=(ROOT/'knowledge/prompts/summarize.md').read_text(encoding='utf-8')
        self.assertIn('先识别事件，再写摘要',text)
        self.assertIn('无法从材料指出两者的事件身份差异',text)
        self.assertIn('不同版本或不同发生时间',text)
        self.assertIn('完全相同的 subject',text)

    def test_long_english_event_identity_is_not_silently_dropped(self):
        identity='Artificial Analysis 发布智能指数 v5'
        self.assertGreater(len(identity),24)
        payload=json.dumps({'topic':'研究','title':'智能指数发布','subject':identity,'bullets':['事实']})
        self.assertEqual(ContentPipeline.parse_summary(payload)[0]['subject'],identity)
