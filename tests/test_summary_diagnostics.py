import json
import unittest
from orchestration.pipeline import ContentPipeline, SummaryValidationError
from orchestration.status import explain_error

class SummaryDiagnosticsTests(unittest.TestCase):
    def test_reasons_do_not_echo_untrusted_content(self):
        secret='private-token-do-not-log'
        cases=[(secret,'json'),(json.dumps({'events':[]}),'count'),
               (json.dumps({'topic':secret}),'topic'),
               (json.dumps({'topic':'研究','bullets':[secret*30]}),'bullets'),
               (json.dumps({'topic':'研究','bullets':['ok'],'title':secret*3}),'title')]
        for payload,code in cases:
            with self.subTest(code=code):
                with self.assertRaises(SummaryValidationError) as caught:
                    ContentPipeline.parse_summary(payload,require_title=True)
                self.assertIn(SummaryValidationError.REASONS[code],caught.exception.detail)
                self.assertNotIn(secret,str(caught.exception))
                if code not in ('json','count'):self.assertIn('事件 1',caught.exception.detail)

    def test_admin_hint_points_to_details(self):
        self.assertIn('阶段记录',explain_error('summarize: SummaryValidationError'))

    def test_stage_records_safe_reason(self):
        from unittest.mock import Mock, patch
        pipeline=object.__new__(ContentPipeline)
        pipeline.store=Mock()
        pipeline.store.task.return_value={'kind':'rss'}
        pipeline.progress_callback=None
        pipeline.summarize=Mock(side_effect=SummaryValidationError('title',2))
        with patch('orchestration.pipeline.log') as logger:
            with self.assertRaises(SummaryValidationError):
                pipeline.traced('summarize')({'task_id':17})
        self.assertIn('事件 2',pipeline.store.event.call_args.args[3])
        self.assertIn('摘要标题无效',logger.call_args.args[0])
