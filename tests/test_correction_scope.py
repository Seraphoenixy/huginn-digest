"""纠错只对 ASR 转写稿有意义：RSS 跳过时要看得见、且不白跑一次模型。"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from config import Settings
from integration.files import write_text
from orchestration.pipeline import ContentPipeline
from orchestration.service import DailyService
from orchestration.status import stage_label


class CorrectionScopeTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='correction-scope-secret-32chars',
                               admin_hash='offline-hash',secure_cookie=False)
        self.service=DailyService(self.settings)
        self.pipeline=ContentPipeline(self.settings,self.service.store)

    def task_of(self,kind,url):
        self.service.enqueue(kind,{'url':url,'title':'标题','published':time.time()})
        return self.service.store.query('SELECT * FROM tasks WHERE url=?',(url,))[0]

    def run_correct(self,kind,url):
        task=self.task_of(kind,url)
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'raw.txt','这是一段正文内容，没有任何代码块。')
        state={'task_id':task['id'],'raw_path':str(folder/'raw.txt')}
        self.pipeline.llm.call=Mock(side_effect=AssertionError('不该调用模型'))
        result=self.pipeline.correct(state)
        return task,result,folder

    def test_rss_never_calls_the_correction_model(self):
        """回归：纠错对文字稿没有可校的东西，调用只会凭空改写专名。"""
        task,result,folder=self.run_correct('rss','https://example.com/rss')
        self.assertEqual(result['corrected_path'],str(folder/'raw.txt'))
        self.assertFalse((folder/'corrected.txt').exists())
        self.assertEqual(self.service.store.task(task['id'])['corrected_path'],str(folder/'raw.txt'))

    def test_bili_still_gets_corrected(self):
        task=self.task_of('bili','https://www.bilibili.com/video/BV1xx411c7mD')
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'raw.txt','这是一段转写稿，没有任何代码块。')
        self.pipeline.llm.call=Mock(return_value='这是一段转写稿，没有任何代码块。')
        result=self.pipeline.correct({'task_id':task['id'],'raw_path':str(folder/'raw.txt')})
        self.assertEqual(result['corrected_path'],str(folder/'corrected.txt'))
        self.pipeline.llm.call.assert_called()

    def test_stage_label_distinguishes_rss_from_asr(self):
        self.assertEqual(stage_label('correct','rss'),'跳过校对')
        self.assertEqual(stage_label('correct','bili'),'校对转写')
        # 其他阶段不受任务类型影响。
        self.assertEqual(stage_label('summarize','rss'),'生成摘要')

    def test_rss_skipped_stage_writes_no_events(self):
        """事件日志里不该出现一条没真正执行的“校对转写”。"""
        task,_,_=self.run_correct('rss','https://example.com/rss2')
        rows=self.service.store.query("SELECT stage FROM events WHERE task_id=?",(task['id'],))
        self.assertEqual([row['stage'] for row in rows if row['stage']=='correct'],[],)

    def test_event_rows_carry_kind_for_labels(self):
        task,_,_=self.run_correct('rss','https://example.com/rss3')
        self.service.store.event(task['id'],'summarize','ok','耗时 1 秒')
        row=self.service.store.recent_events(1)[0]
        self.assertEqual(row['kind'],'rss','事件需带任务类型，界面才能选对标签')


if __name__=='__main__':unittest.main()
