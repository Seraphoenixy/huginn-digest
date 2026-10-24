import io
import json
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock
from config import Settings
from integration.files import write_text
from orchestration.service import DailyService
from orchestration.pipeline import ContentPipeline

class LoggingTests(unittest.TestCase):
    def test_idle_prints_only_once_until_work_arrives(self):
        with tempfile.TemporaryDirectory() as directory:
            service=DailyService(Settings(data_dir=Path(directory)))
            output=io.StringIO()
            with redirect_stdout(output):
                service.tick();service.tick()
            self.assertEqual(output.getvalue().count('等待中'),1)

    def test_stage_completion_publication_and_private_error(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(data_dir=Path(directory))
            service=DailyService(settings)
            service.enqueue('rss',{'url':'https://example.com/item','title':'test','published':time.time()})
            task=service.store.claim()
            write_text(settings.content_dir/str(task['id'])/'raw.txt','private-transcript')
            pipeline=ContentPipeline(settings,service.store)
            pipeline.llm.call=Mock(return_value=json.dumps({'events':[{'topic':'AI 与大模型','title':'日志标题','bullets':['test']}]},ensure_ascii=False))
            output=io.StringIO()
            with redirect_stdout(output):
                pipeline.run(task);service.publish(task['day'])
            text=output.getvalue()
            for expected in ['开始处理','获取正文/转写','无需校对','请求云端摘要','总耗时','发布完成']:
                self.assertIn(expected,text)
            self.assertNotIn('private-transcript',text)
            self.assertRegex(text,r'\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]')
            output=io.StringIO()
            pipeline.graph=Mock()
            pipeline.graph.invoke.side_effect=RuntimeError('private-api-key')
            with redirect_stdout(output):pipeline.run(task)
            self.assertIn('后重试',output.getvalue())
            self.assertNotIn('private-api-key',output.getvalue())
