import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from config import ROOT,Settings
from integration.files import read_text,write_text
from orchestration.pipeline import ContentPipeline,SummaryValidationError
from orchestration.service import DailyService

class SummaryTitleRecoveryTests(unittest.TestCase):
    def test_bad_thirtieth_title_keeps_entire_chunk_and_safe_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'config.toml'
            config.write_text((ROOT/'config.example.toml').read_text(encoding='utf-8'),encoding='utf-8')
            service=DailyService(Settings(config_file=config))
            service.enqueue('rss',{'url':'https://example.com/test','title':'test','published':1})
            task=service.store.claim()
            pipeline=ContentPipeline(service.settings,service.store)
            state={'task_id':task['id']}
            folder=pipeline.folder(state)
            raw=folder/'raw.txt';write_text(raw,'离线材料')
            state['corrected_path']=str(raw)
            secret='do-not-log-title'
            events=[dict(topic='研究',title=f'事件{i}',bullets=[f'事实{i}']) for i in range(30)]
            events[-1]['title']=secret*5
            service.store.update(task['id'],warning='保留原有警告')
            pipeline.llm.call=Mock(return_value=json.dumps({'events':events}))
            with patch('orchestration.pipeline.log') as logger:
                result=pipeline.summarize(state)
            saved=json.loads(read_text(result['summary_path']))['events']
            self.assertEqual(len(saved),30)
            self.assertEqual(saved[-1]['bullets'],['事实29'])
            self.assertTrue(saved[-1]['title'].endswith('…'))
            self.assertLessEqual(len(saved[-1]['title']),40)
            logs=' '.join(call.args[0] for call in logger.call_args_list)
            self.assertIn('事件 30',logs)
            self.assertIn('标题过长',logs)
            self.assertNotIn(secret,logs)
            self.assertIn('保留原有警告',service.store.task(task['id'])['warning'])
            pipeline.llm.call.reset_mock()
            pipeline.summarize(state)
            pipeline.llm.call.assert_not_called()

    def test_title_repair_does_not_allow_invalid_facts(self):
        with self.assertRaises(SummaryValidationError):
            ContentPipeline.parse_summary(json.dumps({'topic':'研究','title':None,'bullets':[]}),require_title=True)
