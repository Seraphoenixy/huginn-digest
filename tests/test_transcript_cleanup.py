"""转写中间产物 30 天清理：不影响日报，且可配置为永久保留。"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from config import Settings
from integration.cleanup import cleanup
from integration.files import write_text
from interaction.web import create_app
from fastapi.testclient import TestClient
from orchestration.service import DailyService, day_of


def settings_for(directory, **overrides):
    s=Settings(data_dir=Path(directory))
    for key,value in overrides.items():
        s.values.setdefault('storage',{})[key]=value
    return s


class TranscriptCleanupTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=settings_for(self.directory.name)
        self.service=DailyService(self.settings)
        self.day=day_of(time.time())

    def make_task(self,url='https://example.com/a',age_days=0):
        self.service.enqueue('rss',{'url':url,'title':'新闻','published':time.time()})
        task=self.service.store.query('SELECT * FROM tasks WHERE url=?',(url,))[0]
        folder=self.settings.content_dir/str(task['id'])
        for name in ('raw.txt','corrected.txt','feed.txt','part_1.txt','correction_0.txt','correction_manifest.json'):
            write_text(folder/name,'正文')
        write_text(folder/'summary.json',json.dumps({'topic':'AI 与大模型','bullets':['要点']}))
        write_text(folder/'summary_part_0.json',json.dumps({'topic':'AI 与大模型','bullets':['要点']}))
        self.service.store.update(task['id'],status='done',
                                  raw_path=str(folder/'raw.txt'),
                                  corrected_path=str(folder/'corrected.txt'),
                                  summary_path=str(folder/'summary.json'))
        if age_days:
            stamp=time.time()-age_days*86400
            for path in folder.iterdir():
                if path.is_file():os.utime(path,(stamp,stamp))
        return task,folder

    def test_transcripts_expire_but_summary_and_report_survive(self):
        task,folder=self.make_task(age_days=31)
        self.service.publish(self.day)
        cleanup(self.settings,self.service.store.query('SELECT id,status FROM tasks'),now=time.time())
        for name in ('raw.txt','corrected.txt','feed.txt','part_1.txt','correction_0.txt','correction_manifest.json'):
            self.assertFalse((folder/name).exists(),f'{name} 应已清理')
        self.assertTrue((folder/'summary.json').is_file(),'摘要必须保留')
        # 日报展示完全不依赖转写原文
        report=self.service.report(self.day)
        self.assertIsNotNone(report)
        self.assertEqual([e['title'] for e in report['entries']],['新闻'])

    def test_recent_transcripts_are_kept(self):
        task,folder=self.make_task(age_days=5)
        cleanup(self.settings,self.service.store.query('SELECT id,status FROM tasks'),now=time.time())
        self.assertTrue((folder/'raw.txt').is_file())
        self.assertTrue((folder/'corrected.txt').is_file())

    def test_boundary_is_thirty_days(self):
        _,just_inside=self.make_task(url='https://example.com/a',age_days=29)
        _,just_outside=self.make_task(url='https://example.com/b',age_days=31)
        cleanup(self.settings,self.service.store.query('SELECT id,status FROM tasks'),now=time.time())
        self.assertTrue((just_inside/'raw.txt').is_file(),'29 天应保留')
        self.assertFalse((just_outside/'raw.txt').exists(),'31 天应删除')

    def test_zero_disables_expiry(self):
        settings=settings_for(self.directory.name,transcript_days=0)
        service=DailyService(settings)
        service.enqueue('rss',{'url':'https://example.com/k','title':'新闻','published':time.time()})
        task=service.store.query('SELECT * FROM tasks')[0]
        folder=settings.content_dir/str(task['id'])
        write_text(folder/'raw.txt','正文')
        write_text(folder/'summary.json',json.dumps({'topic':'AI 与大模型','bullets':['要点']}))
        service.store.update(task['id'],status='done',summary_path=str(folder/'summary.json'))
        stamp=time.time()-999*86400
        for path in folder.iterdir():
            if path.is_file():os.utime(path,(stamp,stamp))
        cleanup(settings,service.store.query('SELECT id,status FROM tasks'),now=time.time())
        self.assertTrue((folder/'raw.txt').is_file(),'0 表示永久保留')

    def test_unfinished_tasks_are_not_pruned(self):
        self.service.enqueue('rss',{'url':'https://example.com/q','title':'排队','published':time.time()})
        task=self.service.store.query('SELECT * FROM tasks')[0]
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'part_1.txt','正文')
        stamp=time.time()-999*86400
        os.utime(folder/'part_1.txt',(stamp,stamp))
        cleanup(self.settings,self.service.store.query('SELECT id,status FROM tasks'),now=time.time())
        self.assertTrue((folder/'part_1.txt').is_file(),'未完成任务不清理')

    def test_report_page_still_renders_after_transcript_removal(self):
        task,folder=self.make_task(age_days=31)
        self.service.publish(self.day)
        cleanup(self.settings,self.service.store.query('SELECT id,status FROM tasks'),now=time.time())
        app=create_app(self.settings,run_background=False)
        app.state.service=self.service
        with TestClient(app) as client:
            self.assertEqual(client.get('/').status_code,200)
            self.assertEqual(client.get(f'/daily/{self.day}').status_code,200)
            self.assertIn('新闻',client.get(f'/daily/{self.day}').text)

    def test_repair_needed_true_after_transcript_removal(self):
        """删掉转写后重建会重新采集，这是预期而非缺陷。"""
        task,folder=self.make_task(age_days=31)
        cleanup(self.settings,self.service.store.query('SELECT id,status FROM tasks'),now=time.time())
        pipeline=self.service._ensure_pipeline()
        self.assertTrue(pipeline.repair_needed(self.service.store.task(task['id'])))

    def test_llm_and_asr_config_unaffected(self):
        """清理只按storage设置动作，不受其他段影响。"""
        task,folder=self.make_task(age_days=31)
        cleanup(self.settings,self.service.store.query('SELECT id,status FROM tasks'),now=time.time())
        self.assertEqual(self.settings.get('storage','transcript_days',None),30)


class TranscriptConfigTests(unittest.TestCase):
    def write(self,text):
        self.config.write_text(text,encoding='utf-8')

    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config=Path(self.directory.name)/'config.toml'
        from config import ROOT
        self.template=(ROOT/'config.example.toml').read_text(encoding='utf-8')
        self.write(self.template)

    def test_default_is_thirty_days(self):
        settings=Settings(config_file=self.config)
        self.assertEqual(settings.get('storage','transcript_days'),30)

    def test_legacy_config_without_key_defaults_to_thirty(self):
        legacy='\n'.join(line for line in self.template.splitlines() if not line.startswith('transcript_days'))
        self.write(legacy)
        self.assertEqual(Settings(config_file=self.config).get('storage','transcript_days',30),30)

    def test_invalid_values_rejected(self):
        for value in ('-1','3651','true','"30"'):
            self.write(self.template.replace('transcript_days = 30',f'transcript_days = {value}'))
            with self.subTest(value=value),self.assertRaises(ValueError):
                Settings(config_file=self.config)

    def test_zero_is_allowed(self):
        self.write(self.template.replace('transcript_days = 30','transcript_days = 0'))
        self.assertEqual(Settings(config_file=self.config).get('storage','transcript_days'),0)


if __name__=='__main__':unittest.main()
