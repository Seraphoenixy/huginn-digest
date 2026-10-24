"""纠错校验：放行切分边界差异，拦住真实篡改；失败原因分开记。"""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from config import Settings
from integration.files import write_text
from orchestration.pipeline import ContentPipeline, CorrectionRejected
from orchestration.service import DailyService


class NumberGuardTests(unittest.TestCase):
    def test_glued_tokens_are_not_treated_as_tampering(self):
        """实测：ASR 把 Opus 5.5 粘成 oppos5.5，纠错补空格后切出 5.5，曾被判篡改。"""
        self.assertTrue(ContentPipeline.same_numbers('oppos5.5 得分92.7%','Opus 5.5 得分 92.7%'))

    def test_merging_two_digits_into_a_decimal_stays_rejected(self):
        """「5 5」→「5.5」在数字层面与「55」→「5.5」无法区分，只能保守拒绝。

        放宽这一条会同时放过 15→1.5、100→1.00 这类真实篡改，
        所以宁可让一次正确纠错回退，也不放行数值改写。
        """
        self.assertFalse(ContentPipeline.same_numbers('5 5','5.5'))
        self.assertFalse(ContentPipeline.same_numbers('55','5.5'))
        self.assertFalse(ContentPipeline.same_numbers('15','1.5'))

    def test_unit_and_sign_changes_are_rejected(self):
        for original,result in (('占比 2%','占比 2'),('降幅 -2%','降幅 2%'),('104B','10.4B')):
            with self.subTest(original=original,result=result):
                self.assertFalse(ContentPipeline.same_numbers(original,result))

    def test_number_changes_are_still_rejected(self):
        for original,result in (('2.8T 总参数','3.8T 总参数'),
                                ('104B','204B'),
                                ('2026-10-24','2026-10-25'),
                                ('5.5','55')):
            with self.subTest(original=original,result=result):
                self.assertFalse(ContentPipeline.same_numbers(original,result))

    def test_identical_text_passes(self):
        self.assertTrue(ContentPipeline.same_numbers('2.8T','2.8T'))

    def test_validate_correction_reports_number_change(self):
        with tempfile.TemporaryDirectory() as directory:
            s=Settings(data_dir=Path(directory),session_secret='guard-secret-at-least-32-chars',
                       admin_hash='h',secure_cookie=False)
            p=ContentPipeline(s,DailyService(s).store)
            with self.assertRaises(ValueError) as raised:
                p.validate_correction('2.8T 总参数','3.8T 总参数')
            self.assertIn('数字',str(raised.exception))


class CorrectionReasonTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='reason-secret-at-least-32-chars',
                               admin_hash='offline-hash',secure_cookie=False)
        self.service=DailyService(self.settings)
        self.pipeline=ContentPipeline(self.settings,self.service.store)
        self.service.enqueue('bili',{'url':'https://www.bilibili.com/video/BV1xx411c7mD',
                                     'title':'标题','published':time.time()})
        self.task=self.service.store.query('SELECT * FROM tasks')[0]
        folder=self.settings.content_dir/str(self.task['id'])
        write_text(folder/'raw.txt','这是转写稿 2.8T 总参数，没有任何代码块。')
        self.state={'task_id':self.task['id'],'raw_path':str(folder/'raw.txt')}

    def warning(self):
        return self.service.store.task(self.task['id'])['warning']

    def test_rejection_says_it_failed_validation_not_the_call(self):
        self.pipeline.llm.call=Mock(return_value='这是转写稿 3.8T 总参数，没有任何代码块。')
        self.pipeline.correct(self.state)
        text=self.warning()
        self.assertIn('未通过校验',text)
        self.assertIn('数字',text)
        self.assertNotIn('调用失败',text)

    def test_call_failure_says_the_call_failed(self):
        self.pipeline.llm.call=Mock(side_effect=RuntimeError('network down'))
        self.pipeline.correct(self.state)
        text=self.warning()
        self.assertIn('调用失败',text)
        self.assertIn('RuntimeError',text)
        self.assertNotIn('未通过校验',text)

    def test_successful_correction_leaves_no_warning(self):
        self.pipeline.llm.call=Mock(return_value='这是转写稿 2.8T 总参数，没有任何代码块。')
        self.pipeline.correct(self.state)
        self.assertEqual(self.warning(),'')

    def test_rejection_keeps_original_and_flags_incomplete(self):
        """校验不过时要保留原文，并且不把纠错稿标成完整。"""
        self.pipeline.llm.call=Mock(return_value='这是转写稿 3.8T 总参数，没有任何代码块。')
        result=self.pipeline.correct(self.state)
        folder=self.settings.content_dir/str(self.task['id'])
        self.assertEqual(Path(result['corrected_path']).name,'raw.txt')
        self.assertFalse((folder/'correction_manifest.json').exists()
                         and __import__('json').loads((folder/'correction_manifest.json').read_text(encoding='utf-8')).get('complete'))


if __name__=='__main__':unittest.main()
