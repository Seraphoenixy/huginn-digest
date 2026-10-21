import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from config import ROOT, Settings
from integration.files import read_text, write_text
from orchestration.pipeline import ContentPipeline,split_text
from orchestration.service import DailyService
from orchestration.terminology import apply_terms


VIDEO='BV1xx411c7mD'
OTHER='BV1yy411c7mD'


class TerminologyTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config=Path(self.temp.name)/'config.toml'
        self.template=(ROOT/'config.example.toml').read_text(encoding='utf-8')
        self.config.write_text(self.template,encoding='utf-8')
        self.settings=Settings(config_file=self.config)
        self.service=DailyService(self.settings)
        self.service.enqueue('bili',{'url':f'https://www.bilibili.com/video/{VIDEO}','title':'术语校正测试','published':1})
        self.task=self.service.store.claim()
        self.pipeline=ContentPipeline(self.settings,self.service.store)
        self.folder=self.pipeline.folder({'task_id':self.task['id']})
        self.raw=self.folder/'raw.txt'
        self.state={'task_id':self.task['id'],'raw_path':str(self.raw)}

    def term(self,name,aliases,video=VIDEO):
        return dict(video=video,name=name,aliases=aliases,evidence='人工确认的名称，非模型自行推测')

    def test_confirmed_five_names_are_corrected_without_changing_raw(self):
        pairs=[('space money','Space Bunny'),('Try','TRAE'),('min3.0','Ming-3.0'),
               ('星4点0219 b intent','Xing4.0-29B-Intent'),('gp. T image2.5 zamberst','GPT-Image-2.5 Sunburst')]
        self.settings.correction_terms=[self.term(name,[alias]) for alias,name in pairs]
        original='。'.join(alias+'发布产品' for alias,_ in pairs)+'。其他事件 70% 不变。'
        write_text(self.raw,original)
        self.pipeline.llm.call=Mock(side_effect=lambda prompt,payload:json.loads(payload)['text'])
        output=self.pipeline.correct(self.state)
        result=read_text(output['corrected_path'])
        for _,name in pairs:self.assertIn(name,result)
        self.assertEqual(read_text(self.raw),original)
        self.assertIn('其他事件 70% 不变',result)
        self.assertTrue(self.pipeline.correction_current(self.raw,self.task))
        self.pipeline.llm.call.reset_mock()
        self.pipeline.correct(self.state)
        self.pipeline.llm.call.assert_not_called()

    def test_numeric_exception_is_limited_to_exact_confirmed_alias(self):
        terms=[self.term('Example-29B',['示例219 b'])]
        baseline=apply_terms('示例219 b，其他 219B，费用 70%。',terms)
        self.assertEqual(baseline,'Example-29B，其他 219B，费用 70%。')
        with self.assertRaises(ValueError):
            self.pipeline.validate_confirmed(baseline,baseline.replace('其他 219B','其他 29B'),terms)
        with self.assertRaises(ValueError):
            self.pipeline.validate_confirmed(baseline,baseline.replace('Example-29B','Another-29B'),terms)

    def test_matching_is_literal_bounded_longest_and_idempotent(self):
        terms=[self.term('ExampleTool',['Old Tool','Old Tool Pro','x.y'])]
        text='Old Tool Pro。Old Tool。x.y。xay。MyOld Tool。'
        result=apply_terms(text,terms)
        self.assertEqual(result,'ExampleTool。ExampleTool。ExampleTool。xay。MyOld Tool。')
        self.assertEqual(apply_terms(result,terms),result)

    def test_confirmed_name_is_not_cut_at_hard_chunk_boundary(self):
        name='Example-29B-Intent'
        original='甲'*995+name+'乙'*100
        chunks=split_text(original,1000,[name])
        self.assertEqual(''.join(chunks),original)
        self.assertTrue(all(len(chunk)<=1000 for chunk in chunks))
        self.assertEqual(sum(name in chunk for chunk in chunks),1)

    def test_terms_are_video_scoped_and_change_cache_signature(self):
        term=self.term('ExampleTool',['Old Tool'])
        self.settings.correction_terms=[term]
        self.assertEqual(self.pipeline.confirmed_terms(dict(self.task,url=OTHER)),[])
        write_text(self.raw,'Old Tool 发布产品。')
        self.pipeline.llm.call=Mock(side_effect=lambda prompt,payload:json.loads(payload)['text'])
        self.pipeline.correct(self.state)
        self.assertTrue(self.pipeline.correction_current(self.raw,self.task))
        self.settings.correction_terms=[self.term('ExampleTool2',['Old Tool'])]
        self.assertFalse(self.pipeline.correction_current(self.raw,self.task))

    def test_model_cannot_undo_confirmed_term_even_when_it_fails(self):
        self.settings.correction_terms=[self.term('ExampleTool',['Old Tool'])]
        original='Old Tool 发布产品，价格 20 元。'
        write_text(self.raw,original)
        self.pipeline.llm.call=Mock(return_value='WrongTool 发布产品，价格 20 元。')
        result=read_text(self.pipeline.correct(self.state)['corrected_path'])
        self.assertEqual(result,'ExampleTool 发布产品，价格 20 元。')
        self.assertEqual(read_text(self.raw),original)
        manifest=json.loads(read_text(self.folder/'correction_manifest.json'))
        self.assertFalse(manifest['complete'])
        self.assertNotIn('ExampleTool',read_text(self.folder/'correction_manifest.json'))

    def test_configuration_validation_rejects_ambiguous_or_unbounded_terms(self):
        valid='\n[[correction.terms]]\nvideo="'+VIDEO+'"\nname="Tool"\naliases=["Old"]\nevidence="人工确认"\n'
        self.config.write_text(self.template+valid,encoding='utf-8')
        self.assertEqual(Settings(config_file=self.config).correction_terms[0]['name'],'Tool')
        bad_cases=[valid.replace(VIDEO,'*'),valid.replace('aliases=["Old"]','aliases=[]'),
                   valid.replace('evidence="人工确认"','evidence=""'),
                   valid+valid.replace('name="Tool"','name="Another"')]
        for entry in bad_cases:
            with self.subTest(entry=entry):
                self.config.write_text(self.template+entry,encoding='utf-8')
                with self.assertRaises(ValueError):Settings(config_file=self.config)
