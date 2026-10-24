"""主题词表可配置：默认覆盖多条内容线，站点可增删并触发重新分类。"""
import json
import tempfile
import unittest
from pathlib import Path

from config import ROOT, Settings
from integration.files import write_text
from orchestration.pipeline import ContentPipeline
from orchestration.service import DailyService
from orchestration.status import topic_name, topic_order


class TopicVocabularyTests(unittest.TestCase):
    def settings(self, extra=''):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path=Path(directory.name)/'config.toml'
        # 示例配置已带 [topics] 段；追加自定义词表前先移除旧段，避免重复声明。
        base=(ROOT/'config.example.toml').read_text(encoding='utf-8')
        if extra:
            start=base.find('\n[topics]')
            if start!=-1:
                end=base.find('\n[',start+1)
                base=base[:start]+('' if end==-1 else base[end:])
        path.write_text(base+extra,encoding='utf-8')
        return Settings(data_dir=Path(directory.name)/'data',config_file=path)

    def test_default_vocabulary_covers_non_ai_content_lines(self):
        """回归：词表原本只有 技术/产品/行业/研究，时事与娱乐只能落进“其他”。"""
        settings=self.settings()
        order=settings.topic_order
        for topic in ('游戏','动漫','影视','时事','开发与开源'):
            self.assertIn(topic,order)
            self.assertEqual(topic_name(topic,order),topic)
        # 兜底项不写进配置，但始终可用且排在最后。
        self.assertNotIn('其他',order)
        self.assertEqual(topic_order(order)[-1],'其他')

    def test_unknown_topic_folds_to_fallback(self):
        settings=self.settings()
        self.assertEqual(topic_name('技术',settings.topic_order),'其他')
        self.assertEqual(topic_name(None,settings.topic_order),'其他')

    def test_section_order_follows_configured_vocabulary(self):
        settings=self.settings('\n[topics]\norder = ["游戏","时事","其他"]\n')
        report={'day':'2026-10-24','entries':[
            {'id':1,'topic':'时事'},{'id':2,'topic':'游戏'},{'id':3,'topic':'不存在'}]}
        sections=DailyService.sections(report,settings.topic_order)
        self.assertEqual([item['topic'] for item in sections],['游戏','时事','其他'])

    def test_prompt_renders_the_configured_vocabulary(self):
        settings=self.settings('\n[topics]\norder = ["游戏","动漫","其他"]\n')
        pipeline=ContentPipeline.__new__(ContentPipeline)
        pipeline.settings=settings
        text=pipeline.prompt('summarize')
        for topic in ('游戏','动漫','其他'):
            self.assertIn(topic,text)
        self.assertNotIn('{topics}',text)
        # 示例值取词表首项，避免提示词里默认成已删除的旧分类。
        self.assertIn('"topic":"游戏"',text)

    def test_parse_summary_rejects_topic_outside_vocabulary(self):
        settings=self.settings()
        ok=json.dumps({'topic':'游戏','title':'标题','bullets':['要点']},ensure_ascii=False)
        self.assertEqual(ContentPipeline.parse_summary(ok,topics=settings.topic_order)[0]['topic'],'游戏')
        bad=json.dumps({'topic':'娱乐八卦','title':'标题','bullets':['要点']},ensure_ascii=False)
        with self.assertRaises(ValueError):
            ContentPipeline.parse_summary(bad,topics=settings.topic_order)

    def test_vocabulary_change_invalidates_cached_summaries(self):
        """词表换掉后旧分类必须重算，否则页面还是旧分节。"""
        settings=self.settings()
        service=DailyService(settings)
        pipeline=service._ensure_pipeline()
        folder=settings.content_dir/'1'
        write_text(folder/'raw.txt','正文')
        before=pipeline.summary_fingerprint(folder/'raw.txt')
        settings.topic_order=('游戏','时事')
        after=pipeline.summary_fingerprint(folder/'raw.txt')
        self.assertNotEqual(before,after)

    def test_invalid_vocabulary_is_rejected_at_startup(self):
        for extra in ('\n[topics]\norder = ["只有一个"]\n',
                      '\n[topics]\norder = ["游戏","游戏"]\n',
                      '\n[topics]\norder = ["游戏",""]\n'):
            with self.subTest(extra=extra.strip()):
                with self.assertRaises(ValueError):
                    self.settings(extra)


if __name__=='__main__':unittest.main()
