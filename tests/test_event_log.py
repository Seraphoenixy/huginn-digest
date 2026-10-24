"""阶段记录：按任务聚合成一轮，并按固定条数分页。"""
import json
import re
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from config import Settings
from integration.auth import hash_password
from interaction.web import create_app
from orchestration.service import DailyService


class EventLogTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='event-log-secret-at-least-32-chars',
                               admin_hash=hash_password('abcdefghij'),secure_cookie=False)
        self.service=DailyService(self.settings)
        self.app=create_app(self.settings,run_background=False)
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)
        self.client.cookies.set('daily_session',self.service.new_session(True))

    def seed_events(self,task_id,stages):
        for stage,status,detail in stages:
            self.service.store.event(task_id,stage,status,detail)

    def test_events_of_one_task_are_grouped_into_a_single_round(self):
        self.seed_events(7,[('acquire','started',''),('acquire','ok','耗时 1 秒'),
                            ('summarize','started',''),('summarize','ok','耗时 2 秒')])
        log=self.service.event_log()
        self.assertEqual(len(log['groups']),1,'同一任务的多个阶段应合成一轮')
        group=log['groups'][0]
        self.assertEqual(group['task_id'],7)
        self.assertEqual([stage['label'] for stage in group['stages']],['获取正文','生成摘要'])
        self.assertEqual(group['state'],'ok')

    def test_failed_stage_marks_the_round_failed(self):
        self.seed_events(8,[('summarize','started',''),('summarize','CloudError','云端模型调用失败')])
        group=self.service.event_log()['groups'][0]
        self.assertEqual(group['state'],'danger')
        self.assertEqual(group['state_label'],'失败')
        self.assertEqual(group['stages'][-1]['detail'],'云端模型调用失败')

    def test_unfinished_stage_shows_as_running(self):
        self.seed_events(9,[('acquire','started',''),('acquire','ok','耗时 1 秒'),('correct','started','')])
        group=self.service.event_log()['groups'][0]
        self.assertEqual(group['state'],'run')
        self.assertEqual(group['stages'][-1]['state'],'run')

    def test_events_are_paginated_with_a_stable_page_size(self):
        for index in range(45):
            self.seed_events(100+index,[('finish','ok','')])
        log=self.service.event_log()
        self.assertEqual(log['total'],45)
        self.assertEqual(log['pages'],3,'45 条按每页 20 条分成 3 页')
        self.assertEqual(log['page'],1)
        self.assertLessEqual(len(log['groups']),20)
        second=self.service.event_log(2)
        self.assertEqual(second['page'],2)
        self.assertNotEqual([g['task_id'] for g in log['groups']],
                           [g['task_id'] for g in second['groups']],'相邻两页内容不重复')

    def test_page_number_is_clamped(self):
        self.seed_events(1,[('finish','ok','')])
        self.assertEqual(self.service.event_log(99)['page'],1)
        self.assertEqual(self.service.event_log(0)['page'],1)
        self.assertEqual(self.service.event_log('abc')['page'],1)

    def test_page_renders_grouped_rows_and_pager(self):
        for index in range(25):
            self.seed_events(index+1,[('acquire','started',''),('acquire','ok','耗时 1 秒')])
        page=self.client.get('/admin').text
        self.assertIn('阶段记录',page)
        # 25 组 × 2 条事件 = 50 条，每页 20 条 → 3 页。
        self.assertIn('共 50 条 · 第 1/3 页',page)
        self.assertIn('runlog__group',page)
        self.assertIn('&events=2',page)
        second=self.client.get('/admin?events=2').text
        self.assertIn('第 2 / 3 页',second)
        # 分页链接保留当前视图与日期过滤，不会把用户带回默认视图。
        filtered=self.client.get(f'/admin?view=failed&events=2').text
        self.assertIn('view=failed',filtered)


if __name__=='__main__':unittest.main()
