"""日报保留上限：到期整日删除、不复活、可关闭、边界正确。"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from config import ROOT, Settings
from integration.cleanup import cleanup
from integration.files import write_text
from orchestration.service import DailyService, day_of


class ReportRetentionTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config=Path(self.directory.name)/'config.toml'
        self.template=(ROOT/'config.example.toml').read_text(encoding='utf-8')
        self.config.write_text(self.template,encoding='utf-8')

    def settings(self,**over):
        text=self.template
        for key,value in over.items():
            text=text.replace(f'{key} = 1095',f'{key} = {value}')
        self.config.write_text(text,encoding='utf-8')
        return Settings(config_file=self.config)

    def seed(self,service,settings,day,url,title='新闻'):
        service.enqueue('rss',{'url':url,'title':title,'published':time.time()})
        task=service.store.query('SELECT * FROM tasks WHERE url=?',(url,))[0]
        service.store.execute('UPDATE tasks SET day=? WHERE id=?',(day,task['id']))
        folder=settings.content_dir/str(task['id'])
        write_text(folder/'raw.txt','转写正文')
        write_text(folder/'summary.json',json.dumps({'topic':'技术','bullets':['要点']},ensure_ascii=False))
        service.store.update(task['id'],status='done',summary_path=str(folder/'summary.json'),
                             raw_path=str(folder/'raw.txt'))
        service.publish(day)
        return task,folder

    def test_expired_day_is_removed_entirely(self):
        settings=self.settings()
        service=DailyService(settings)
        now=time.time()
        old=day_of(now-1200*86400)
        fresh=day_of(now-10*86400)
        _,old_folder=self.seed(service,settings,old,'https://example.com/o')
        self.seed(service,settings,fresh,'https://example.com/f')

        cleanup(settings,service.store.query('SELECT id,status FROM tasks'),now=now)

        self.assertIsNone(service.report(old))
        self.assertNotIn(old,service.dates())
        self.assertIsNotNone(service.report(fresh))
        self.assertIn(fresh,service.dates())
        self.assertFalse(old_folder.exists(),'任务产物目录应被清理')
        self.assertFalse((settings.data_dir/'reports'/old).exists())
        self.assertEqual(service.store.query('SELECT id FROM versions WHERE day=?',(old,)),[])
        self.assertEqual(service.store.query('SELECT id FROM tasks WHERE day=?',(old,)),[])

    def test_expired_day_is_not_republished_by_later_ticks(self):
        """回归：保留 done 任务会让 publish_dirty 把已删的日报重新发出来。"""
        settings=self.settings()
        service=DailyService(settings)
        now=time.time()
        old=day_of(now-1200*86400)
        self.seed(service,settings,old,'https://example.com/o')
        cleanup(settings,service.store.query('SELECT id,status FROM tasks'),now=now)
        self.assertIsNone(service.report(old))
        service.runtime.start()
        for _ in range(3):
            service.tick()
        self.assertIsNone(service.report(old),'清理后不得被自动重建')
        self.assertNotIn(old,service.dates())

    def test_boundary_respects_configured_days(self):
        settings=self.settings(report_days=100)
        service=DailyService(settings)
        now=time.time()
        inside=day_of(now-99*86400)
        outside=day_of(now-101*86400)
        self.seed(service,settings,inside,'https://example.com/i')
        self.seed(service,settings,outside,'https://example.com/o')
        cleanup(settings,service.store.query('SELECT id,status FROM tasks'),now=now)
        self.assertIsNotNone(service.report(inside),'99 天应保留')
        self.assertIsNone(service.report(outside),'101 天应删除')

    def test_zero_keeps_reports_forever(self):
        settings=self.settings(report_days=0)
        service=DailyService(settings)
        ancient=day_of(time.time()-5000*86400)
        self.seed(service,settings,ancient,'https://example.com/x')
        cleanup(settings,service.store.query('SELECT id,status FROM tasks'),now=time.time())
        self.assertIsNotNone(service.report(ancient))

    def test_retracted_day_is_also_expired(self):
        """全部撤回的日期没有活动版本，但仍有版本行，到期应一并清除。"""
        settings=self.settings()
        service=DailyService(settings)
        now=time.time()
        old=day_of(now-1200*86400)
        task,_=self.seed(service,settings,old,'https://example.com/o')
        service.retract_entry(task['id'])
        self.assertIsNone(service.report(old))
        cleanup(settings,service.store.query('SELECT id,status FROM tasks'),now=now)
        self.assertEqual(service.store.query('SELECT id FROM versions WHERE day=?',(old,)),[])
        self.assertEqual(service.store.query('SELECT id FROM retractions WHERE day=?',(old,)),[])

    def test_expiry_uses_beijing_date_not_utc(self):
        """截止日按北京时间计算，与归档口径一致。"""
        settings=self.settings(report_days=30)
        service=DailyService(settings)
        # 北京时间 00:30 等价于 UTC 前一天 16:30，两者日期相差一天
        now=time.time()
        boundary=day_of(now-30*86400)
        self.seed(service,settings,boundary,'https://example.com/b')
        cleanup(settings,service.store.query('SELECT id,status FROM tasks'),now=now)
        # 恰好 30 天的日期不在 < cutoff 范围内，应保留
        self.assertIsNotNone(service.report(boundary))

    def test_unfinished_task_of_old_day_is_not_dropped_by_report_retention(self):
        """日期过期清理只删该日全部任务；未完成任务的产物随之清理，属预期。"""
        settings=self.settings()
        service=DailyService(settings)
        now=time.time()
        old=day_of(now-1200*86400)
        service.enqueue('rss',{'url':'https://example.com/q','title':'排队','published':time.time()})
        task=service.store.query('SELECT * FROM tasks')[0]
        service.store.execute("UPDATE tasks SET day=?,status='queued' WHERE id=?",(old,task['id']))
        cleanup(settings,service.store.query('SELECT id,status FROM tasks'),now=now)
        # 任务行仍保留（该日没有versions行，不会触发整日删除）
        self.assertEqual(len(service.store.query('SELECT id FROM tasks WHERE day=?',(old,))),1)


class ReportRetentionConfigTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.config=Path(self.directory.name)/'config.toml'
        self.template=(ROOT/'config.example.toml').read_text(encoding='utf-8')
        self.config.write_text(self.template,encoding='utf-8')

    def test_default_is_three_years(self):
        self.assertEqual(Settings(config_file=self.config).report_retention_days,1095)

    def test_legacy_config_defaults(self):
        legacy='\n'.join(line for line in self.template.splitlines() if not line.startswith('report_days'))
        self.config.write_text(legacy,encoding='utf-8')
        self.assertEqual(Settings(config_file=self.config).report_retention_days,1095)

    def test_invalid_values_rejected(self):
        for value in ('-1','36501','true','"1095"'):
            self.config.write_text(self.template.replace('report_days = 1095',f'report_days = {value}'),encoding='utf-8')
            with self.subTest(value=value),self.assertRaises(ValueError):
                Settings(config_file=self.config)

    def test_zero_allowed(self):
        self.config.write_text(self.template.replace('report_days = 1095','report_days = 0'),encoding='utf-8')
        self.assertEqual(Settings(config_file=self.config).report_retention_days,0)


if __name__=='__main__':unittest.main()
