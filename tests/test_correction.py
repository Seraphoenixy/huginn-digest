import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from config import ROOT,Settings
from integration.files import write_text,read_text
from integration.cleanup import cleanup
from orchestration.service import DailyService
from orchestration.pipeline import ContentPipeline


class CorrectionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config=Path(self.temp.name)/'config.toml'
        self.template=(ROOT/'config.example.toml').read_text(encoding='utf-8')
        self.config.write_text(self.template,encoding='utf-8')
        self.settings=Settings(config_file=self.config)
        self.service=DailyService(self.settings)
        self.service.enqueue('bili',{'url':'https://www.bilibili.com/video/BV1xx411c7mD','title':'GitHub 与 WorkBuddy 的开发功能介绍','published':1})
        self.task=self.service.store.claim()
        self.pipeline=ContentPipeline(self.settings,self.service.store)
        self.state={'task_id':self.task['id']}
        self.folder=self.pipeline.folder(self.state)
        self.raw=self.folder/'raw.txt'
        self.original='Kit Hub 提供代码托管功能。Workbody 支持开发任务，版本为 2.1，可能需要登录。'
        write_text(self.raw,self.original)
        self.state['raw_path']=str(self.raw)

    def identity_model(self,system,payload,*args):
        data=json.loads(payload)
        return data['text']

    def correct(self):
        return self.pipeline.correct(self.state)

    def manifest(self):
        return json.loads(read_text(self.folder/'correction_manifest.json'))

    def test_single_pass_receives_original_and_video_context(self):
        final=self.original.replace('Kit Hub','GitHub').replace('Workbody','WorkBuddy')
        self.pipeline.llm.call=Mock(return_value=final)
        result=self.correct()
        self.assertEqual(read_text(result['corrected_path']),final)
        self.assertEqual(read_text(self.raw),self.original)
        self.pipeline.llm.call.assert_called_once()
        initial=json.loads(self.pipeline.llm.call.call_args.args[1])
        self.assertEqual(initial['text'],self.original)
        self.assertEqual(initial['video_title'],self.task['title'])
        self.assertTrue(self.manifest()['complete'])
        self.assertNotIn(self.original,read_text(self.folder/'correction_manifest.json'))

    def test_chunks_receive_bounded_neighbors_without_duplicating_context(self):
        self.settings.values['correction']['chunk_chars']=1000
        self.settings.values['correction']['context_chars']=40
        raw=('前文介绍 Kit Hub 的代码托管功能。'*100)+'末尾独立事件。'
        write_text(self.raw,raw)
        self.pipeline.llm.call=Mock(side_effect=self.identity_model)
        result=self.correct()
        initial=[json.loads(call.args[1]) for call in self.pipeline.llm.call.call_args_list]
        self.assertGreater(len(initial),1)
        self.assertEqual(''.join(part['text'] for part in initial),raw)
        self.assertTrue(all(len(part['text'])<=1000 and len(part['context_before'])<=40 and len(part['context_after'])<=40 for part in initial))
        self.assertEqual(initial[0]['context_after'],initial[1]['text'][:40])
        self.assertEqual(initial[1]['context_before'],initial[0]['text'][-40:])
        self.assertEqual(re.sub(r'\s+','',read_text(result['corrected_path'])),re.sub(r'\s+','',raw))

    def test_current_cache_reused_and_prompt_change_archives_old_correction(self):
        self.pipeline.llm.call=Mock(side_effect=self.identity_model)
        self.correct()
        self.pipeline.llm.call.reset_mock()
        self.correct()
        self.pipeline.llm.call.assert_not_called()
        original_prompt=self.pipeline.prompt
        self.pipeline.prompt=lambda name:original_prompt(name)+('\n新的校对要求' if name=='correct' else '')
        self.correct()
        self.assertEqual(self.pipeline.llm.call.call_count,1)
        archived=list(self.folder.glob('previous_correction_*'))
        self.assertEqual(len(archived),1)
        self.assertTrue((archived[0]/'corrected.txt').is_file())
        self.assertEqual(read_text(self.raw),self.original)

    def test_legacy_correction_without_manifest_is_not_silently_reused(self):
        write_text(self.folder/'corrected.txt','旧版纠错稿')
        write_text(self.folder/'correction_0.txt','旧版分片')
        self.pipeline.llm.call=Mock(side_effect=self.identity_model)
        result=self.correct()
        self.assertEqual(self.pipeline.llm.call.call_count,1)
        self.assertEqual(read_text(result['corrected_path']),self.original)
        self.assertEqual(read_text(next(self.folder.glob('previous_correction_*'))/'corrected.txt'),'旧版纠错稿')

    def test_first_pass_omission_or_number_changes_fall_back_to_original(self):
        for bad in ('摘要。',self.original.replace('2.1','2.2'),'```text\n'+self.original+'\n```'):
            with self.subTest(result=bad):
                self.pipeline.llm.call=Mock(return_value=bad)
                result=self.correct()
                self.assertEqual(read_text(result['corrected_path']),self.original)
                self.assertIn('纠错失败',self.service.store.task(self.task['id'])['warning'])
                self.assertFalse(self.manifest()['complete'])

    def test_transport_failure_falls_back_without_leaking_error(self):
        import contextlib,io
        self.pipeline.llm.call=Mock(side_effect=RuntimeError('private-api-secret'))
        output=io.StringIO()
        with contextlib.redirect_stdout(output):result=self.correct()
        self.assertEqual(read_text(result['corrected_path']),self.original)
        self.assertNotIn('private-api-secret',output.getvalue()+self.service.store.task(self.task['id'])['warning'])
        self.assertFalse(self.manifest()['complete'])

    def test_main_switch_ignores_old_output(self):
        self.pipeline.llm.call=Mock(side_effect=self.identity_model)
        self.correct()
        self.pipeline.llm.call.assert_called_once()
        self.settings.pipeline_correction=False
        self.pipeline.llm.call.reset_mock()
        self.assertEqual(self.correct()['corrected_path'],str(self.raw))
        self.pipeline.llm.call.assert_not_called()

    def test_uncertain_name_warning_is_count_only(self):
        uncertain=self.original.replace('Workbody','Workbody【待核】')
        self.pipeline.llm.call=Mock(return_value=uncertain)
        result=self.correct()
        self.assertIn('【待核】',read_text(result['corrected_path']))
        warning=self.service.store.task(self.task['id'])['warning']
        self.assertIn('1 处待核词',warning)
        self.assertNotIn('Workbody',warning)

    def test_raw_title_model_and_chunk_policy_changes_invalidate_cache(self):
        self.pipeline.llm.call=Mock(side_effect=self.identity_model)
        self.correct()
        self.assertTrue(self.pipeline.correction_current(self.raw,self.task))
        task=dict(self.task,title='标题变更')
        self.assertFalse(self.pipeline.correction_current(self.raw,task))
        model=self.settings.values['llm']['model']
        self.settings.values['llm']['model']='different-offline-model'
        self.assertFalse(self.pipeline.correction_current(self.raw,self.task))
        self.settings.values['llm']['model']=model
        self.settings.values['correction']['chunk_chars']=2000
        self.assertFalse(self.pipeline.correction_current(self.raw,self.task))
        self.settings.values['correction']['chunk_chars']=4000
        write_text(self.raw,self.original+'新增句子。')
        self.assertFalse(self.pipeline.correction_current(self.raw,self.task))

    def test_rebuild_checks_correction_signature_and_keeps_manifest(self):
        self.pipeline.llm.call=Mock(side_effect=self.identity_model)
        corrected=self.correct()
        self.pipeline.llm.call=Mock(return_value=json.dumps({'topic':'技术','bullets':['离线摘要']}))
        self.pipeline.summarize(self.state|corrected)
        self.assertFalse(self.pipeline.repair_needed(self.task))
        self.assertTrue((self.folder/'correction_manifest.json').exists())
        original_prompt=self.pipeline.prompt
        self.pipeline.prompt=lambda name:original_prompt(name)+('\n更新专名规则' if name=='correct' else '')
        self.assertTrue(self.pipeline.repair_needed(self.task))

    def test_cleanup_preserves_current_manifest_and_only_cleans_done_archives(self):
        self.pipeline.llm.call=Mock(side_effect=self.identity_model)
        self.correct()
        archive=self.folder/'previous_correction_fixture'
        archive.mkdir()
        old=archive/'corrected.txt';write_text(old,'历史稿');os.utime(old,(1,1))
        draft=self.folder/'correction_0.txt';os.utime(draft,(1,1))
        cleanup(self.settings,[{'id':self.task['id'],'status':'queued'}],now=1000000)
        self.assertTrue(old.exists())
        cleanup(self.settings,[{'id':self.task['id'],'status':'done'}],now=1000000)
        self.assertFalse(old.exists())
        self.assertFalse(draft.exists())
        self.assertTrue((self.folder/'corrected.txt').exists())
        self.assertTrue((self.folder/'correction_manifest.json').exists())

    def test_config_defaults_legacy_and_invalid_values(self):
        legacy=re.sub(r'\[correction\]\n.*?(?=\[asr\])','',self.template,flags=re.S)
        self.config.write_text(legacy,encoding='utf-8')
        settings=Settings(config_file=self.config)
        self.assertEqual(settings.correction_min_length_ratio,0.75)
        for key,value in [('chunk_chars','true'),('context_chars','-1'),('min_length_ratio','0.2')]:
            section=re.sub(rf'^{key} = .*$',f'{key} = {value}',self.template[self.template.index('[correction]'):self.template.index('[asr]')],flags=re.M)
            template=self.template[:self.template.index('[correction]')]+section+self.template[self.template.index('[asr]'):]
            self.config.write_text(template,encoding='utf-8')
            with self.subTest(key=key),self.assertRaises(ValueError):Settings(config_file=self.config)

    def test_graph_keeps_text_on_disk_and_reuses_completed_correction_after_chunk_cleanup(self):
        def model(system,payload,json_mode=False):
            if json_mode:return json.dumps({'topic':'技术','bullets':['离线要点']})
            return self.identity_model(system,payload)
        self.pipeline.llm.call=Mock(side_effect=model)
        result=self.pipeline.graph.invoke({'task_id':self.task['id']})
        self.assertEqual(set(result),{'task_id','raw_path','corrected_path','summary_path'})
        self.assertEqual(self.pipeline.llm.call.call_count,2)
        self.assertEqual(self.service.store.task(self.task['id'])['status'],'done')
        for path in self.folder.glob('correction_*.txt'):path.unlink()
        self.pipeline.llm.call.reset_mock()
        self.pipeline.graph.invoke({'task_id':self.task['id']})
        self.pipeline.llm.call.assert_not_called()
        self.assertFalse(self.pipeline.repair_needed(self.task))

    def test_digit_order_signs_and_percentages_cannot_be_silently_changed(self):
        for original,result in [('甲 2，乙 3。','甲 3，乙 2。'),('降幅 -2%。','降幅 2%。'),('占比 2%。','占比 2。')]:
            with self.subTest(original=original),self.assertRaises(ValueError):
                self.pipeline.validate_correction(original,result)

    def test_hard_split_does_not_insert_newline_inside_version_number(self):
        self.settings.values['correction']['chunk_chars']=1000
        original='甲'*998+'12345678'+'乙'*100
        write_text(self.raw,original)
        self.pipeline.llm.call=Mock(side_effect=self.identity_model)
        result=self.correct()
        self.assertEqual(read_text(result['corrected_path']),original)
        self.assertTrue(self.manifest()['complete'])

    def test_numeric_format_equivalence_and_real_value_protection(self):
        for original,result in [('GPT6 版本 2.1。','GPT-6 版本 2.1。'),('数10万用户，超1半。','数十万用户，超一半。'),('增长＋2％。','增长+2%。')]:
            with self.subTest(original=original):
                self.pipeline.validate_correction(original,result)
        for original,result in [('模型 219B。','模型 29B。'),('数10万用户。','数十亿用户。'),('下降 -2%。','下降 +2%。'),('版本 4.0。','版本 4.1。')]:
            with self.subTest(original=original),self.assertRaises(ValueError):
                self.pipeline.validate_correction(original,result)

    def test_old_review_setting_cannot_trigger_second_model_call(self):
        self.config.write_text(self.template.replace('[correction]','[correction]\nreview = true'),encoding='utf-8')
        settings=Settings(config_file=self.config)
        self.pipeline.settings=settings
        uncertain=self.original+'【待核】'
        self.pipeline.llm.call=Mock(return_value=uncertain)
        self.assertEqual(read_text(self.correct()['corrected_path']),uncertain)
        self.pipeline.llm.call.assert_called_once()
        self.assertTrue(self.manifest()['complete'])

    def test_failed_chunk_preserves_other_corrections_and_retries_only_failure(self):
        self.settings.values['correction']['chunk_chars']=1000
        first='原词介绍。'*200
        second='另一事件 29B。'
        write_text(self.raw,first+second)
        fixed=first.replace('原词','正词')
        self.pipeline.llm.call=Mock(side_effect=[fixed,second.replace('29B','219B')])
        result=read_text(self.correct()['corrected_path'])
        self.assertIn('正词介绍',result)
        self.assertIn('29B',result)
        self.assertFalse(self.manifest()['complete'])
        self.pipeline.llm.call=Mock(return_value=second)
        self.correct()
        self.pipeline.llm.call.assert_called_once()
        self.assertTrue(self.manifest()['complete'])
