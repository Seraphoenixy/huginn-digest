"""来源名快照的忠实性：删除或改名后，历史条目不能被改写。"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from config import Settings
from integration.files import write_text
from orchestration.service import DailyService


def settings_for(directory):
    return Settings(data_dir=Path(directory),
                    session_secret='source-name-secret-more-than-32-chars',
                    admin_hash='offline-hash', secure_cookie=False)


class SourceNameSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service=DailyService(settings_for(self.directory.name))
        self.addCleanup(patch.stopall)
        patch('orchestration.service.public_url',side_effect=lambda url:url).start()
        self.source=self.service.add_source('rss','https://example.com/feed.xml','原始来源名')

    def make_done(self,title='文章甲',url='https://example.com/a',bullets=('要点',)):
        self.service.enqueue('rss',{'url':url,'title':title,'published':time.time()},self.source)
        task=self.service.store.query('SELECT * FROM tasks WHERE url=?',(url,))[0]
        folder=self.service.settings.content_dir/str(task['id'])
        write_text(folder/'raw.txt','正文')
        write_text(folder/'summary.json',json.dumps({'topic':'AI 与大模型','bullets':list(bullets)}))
        self.service.store.update(task['id'],status='done',summary_path=str(folder/'summary.json'))
        return task

    def regenerate(self,task,bullets=('新要点',)):
        """模拟重建后摘要重新生成，触发该日重新发布。"""
        folder=self.service.settings.content_dir/str(task['id'])
        write_text(folder/'summary.json',json.dumps({'topic':'AI 与大模型','bullets':list(bullets)}))

    def test_deleted_source_keeps_name_after_republish(self):
        """回归：删除来源后重新发布，历史条目曾降级为“已删除的订阅来源”。"""
        task=self.make_done()
        day=task['day']
        self.service.publish(day)
        self.assertEqual(self.service.report(day)['entries'][0]['source_name'],'原始来源名')

        self.service.source_action(self.source,'delete')
        self.regenerate(task)
        self.service.publish(day,force=True)
        self.assertEqual(self.service.report(day)['entries'][0]['source_name'],'原始来源名')

        # 新版本也必须是真名，不能只是活动指针恰好指向旧版本。
        active=self.service.store.latest(day)
        payload=json.loads(Path(active['path']).read_text(encoding='utf-8'))
        self.assertEqual(payload['entries'][0]['source_name'],'原始来源名')
        self.assertEqual(payload['entries'][0]['bullets'],['新要点'])

    def test_renamed_source_does_not_rewrite_history(self):
        """改名同样不应改写已发布条目的来源名。"""
        task=self.make_done()
        day=task['day']
        self.service.publish(day)
        self.service.edit_source(self.source,'rss','https://example.com/feed.xml','改名后的来源')
        self.regenerate(task)
        self.service.publish(day,force=True)
        self.assertEqual(self.service.report(day)['entries'][0]['source_name'],'原始来源名')

    def test_new_entry_on_same_day_uses_current_name(self):
        """新增条目不是历史，必须使用当前来源名，不能被旧快照覆盖。"""
        first=self.make_done()
        day=first['day']
        self.service.publish(day)
        self.service.edit_source(self.source,'rss','https://example.com/feed.xml','改名后的来源')
        second=self.make_done(title='文章乙',url='https://example.com/b')
        self.service.publish(day,force=True)
        names={entry['title']:entry['source_name'] for entry in self.service.report(day)['entries']}
        self.assertEqual(names['文章甲'],'原始来源名')
        self.assertEqual(names['文章乙'],'改名后的来源')

    def test_legacy_version_without_source_name_falls_back(self):
        """旧版本缺少 source_name 时不崩溃，回退到当前推导。"""
        task=self.make_done()
        day=task['day']
        self.service.publish(day)
        active=self.service.store.latest(day)
        payload=json.loads(Path(active['path']).read_text(encoding='utf-8'))
        payload['entries'][0].pop('source_name')
        write_text(Path(active['path']),json.dumps(payload))
        self.service.source_action(self.source,'delete')
        self.regenerate(task)
        self.service.publish(day,force=True)
        self.assertEqual(self.service.report(day)['entries'][0]['source_name'],'已删除的订阅来源')

    def test_source_name_is_read_before_entries_are_built(self):
        """来源名快照必须取自上一条活动版本，而不是本轮新构造的条目。"""
        task=self.make_done()
        day=task['day']
        self.service.publish(day)
        self.service.source_action(self.source,'delete')
        self.regenerate(task)
        historical={task['id']:'原始来源名'}
        self.assertEqual(self.service._entry_source_name(task,historical),'原始来源名')
        self.assertEqual(self.service._entry_source_name(task,{}),'已删除的订阅来源')


if __name__=='__main__':unittest.main()
