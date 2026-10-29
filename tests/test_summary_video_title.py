import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from config import ROOT,Settings
from integration.files import read_text,write_text
from orchestration.pipeline import ContentPipeline,SummaryValidationError
from orchestration.service import DailyService

class SummaryVideoTitleTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config=Path(self.temp.name)/'config.toml'
        config.write_text((ROOT/'config.example.toml').read_text(encoding='utf-8'),encoding='utf-8')
        self.service=DailyService(Settings(config_file=config))
        self.title='第83期 开源音乐播放器 Folia-major'
        self.service.enqueue('bili',{'url':'https://www.bilibili.com/video/BV1xx411c7mD','title':self.title,'published':1})
        self.task=self.service.store.claim()
        self.pipeline=ContentPipeline(self.service.settings,self.service.store)
        self.state={'task_id':self.task['id']}
        self.folder=self.pipeline.folder(self.state)
        self.raw=self.folder/'raw.txt'
        write_text(self.raw,'four major 是免费音乐播放器，支持 20 个列表。')
        write_text(self.folder/'video_description.txt','开源播放器介绍')
        self.state['raw_path']=str(self.raw)

    def payload(self,name):
        return json.dumps({'events':[{'topic':'产品与应用','title':name+' 音乐播放器','subject':name,'bullets':[name+' 支持播放音乐。']}]})

    def test_title_survives_rejected_correction_and_wrong_summary_is_not_cached(self):
        self.pipeline.llm.call=Mock(return_value='four major 是免费音乐播放器，支持 99 个列表。')
        self.state.update(self.pipeline.correct(self.state))
        self.assertEqual(read_text(self.state['corrected_path']),read_text(self.raw))
        self.pipeline.llm.call=Mock(return_value=self.payload('four major'))
        with self.assertRaises(SummaryValidationError) as caught:self.pipeline.summarize(self.state)
        self.assertIn('第 1/1 段',caught.exception.detail)
        self.assertIn(self.title,self.pipeline.llm.call.call_args.args[1])
        self.assertFalse((self.folder/'summary_part_0.json').exists())
        self.assertFalse((self.folder/'summary.json').exists())
        self.pipeline.llm.call=Mock(return_value=self.payload('Folia-major'))
        self.pipeline.summarize(self.state)
        self.assertIn('Folia-major',read_text(self.folder/'summary.json'))
        self.assertIn('four major',read_text(self.raw))

    def test_title_change_invalidates_summary_cache(self):
        before=self.pipeline.summary_fingerprint(self.raw)
        self.pipeline.store.task=Mock(return_value=dict(self.task,title='另一个播放器 Other-player'))
        self.assertNotEqual(before,self.pipeline.summary_fingerprint(self.raw))

    def test_multi_event_or_ambiguous_title_does_not_force_a_name(self):
        event={'title':'其他消息','bullets':['事实']}
        ContentPipeline.validate_title_entity([event,event],self.title)
        ContentPipeline.validate_title_entity([event],'Folia-major 与 Other-player 对比')
        ContentPipeline.validate_title_entity([event],'普通视频标题')

    def test_name_requires_complete_word_and_accepts_case_variants(self):
        event={'title':'FOLIA-MAJOR 音乐播放器','bullets':['音乐播放']}
        ContentPipeline.validate_title_entity([event],self.title)
        event['title']='NotFolia-majorExtra'
        with self.assertRaises(SummaryValidationError):
            ContentPipeline.validate_title_entity([event],self.title)
