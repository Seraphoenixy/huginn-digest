"""归档导航：年月分组、折叠状态、公开接口契约不变。"""
import json
import re
import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient
from config import Settings
from integration.files import write_text
from interaction.web import create_app
from orchestration.service import DailyService


class ArchiveTreeTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='archive-secret-more-than-32-chars',
                               admin_hash='offline-hash',secure_cookie=False)
        self.service=DailyService(self.settings)

    def publish_day(self,day):
        url=f'https://example.com/{day}'
        self.service.enqueue('rss',{'url':url,'title':f'文章 {day}','published':time.time()})
        task=self.service.store.query('SELECT * FROM tasks WHERE url=?',(url,))[0]
        self.service.store.execute('UPDATE tasks SET day=? WHERE id=?',(day,task['id']))
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'summary.json',json.dumps({'topic':'技术','bullets':['要点']},ensure_ascii=False))
        self.service.store.update(task['id'],status='done',summary_path=str(folder/'summary.json'))
        self.service.publish(day)

    def test_groups_by_year_then_month_descending(self):
        for day in ('2025-12-01','2026-09-30','2026-10-23','2026-10-24'):
            self.publish_day(day)
        tree=self.service.archive()
        self.assertEqual([node['year'] for node in tree],['2026','2025'])
        self.assertEqual([m['month'] for m in tree[0]['months']],['10','09'])
        self.assertEqual(tree[0]['months'][0]['days'],['2026-10-24','2026-10-23'])
        self.assertEqual(tree[0]['count'],3)
        self.assertEqual(tree[1]['count'],1)

    def test_no_active_day_means_everything_collapsed(self):
        for day in ('2025-12-01','2026-10-24'):
            self.publish_day(day)
        tree=self.service.archive()
        self.assertFalse(any(node['open'] for node in tree))
        self.assertFalse(any(m['open'] for node in tree for m in node['months']))

    def test_only_current_year_and_month_open(self):
        for day in ('2025-12-01','2026-09-30','2026-10-24'):
            self.publish_day(day)
        tree=self.service.archive('2026-10-24')
        opened_years=[node['year'] for node in tree if node['open']]
        self.assertEqual(opened_years,['2026'])
        opened_months=[(node['year'],m['month']) for node in tree for m in node['months'] if m['open']]
        self.assertEqual(opened_months,[('2026','10')])

    def test_active_day_opens_its_own_month_not_latest(self):
        for day in ('2026-09-30','2026-10-24'):
            self.publish_day(day)
        tree=self.service.archive('2026-09-30')
        months=tree[0]['months']
        self.assertEqual([m['month'] for m in months if m['open']],['09'])

    def test_empty_archive(self):
        self.assertEqual(self.service.archive(),[])

    def test_dates_contract_is_unchanged(self):
        """公开 /api/public/days 仍是扁平数组，不因侧栏改造而变。"""
        for day in ('2026-10-23','2026-10-24'):
            self.publish_day(day)
        self.assertEqual(self.service.dates(),['2026-10-24','2026-10-23'])
        self.assertIsInstance(self.service.dates()[0],str)


class ArchiveLayoutTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='archive-layout-secret-32-chars-min',
                               admin_hash='offline-hash',secure_cookie=False)
        self.app=create_app(self.settings,run_background=False)
        self.service=self.app.state.service
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)
        for day in ('2025-12-01','2026-09-30','2026-10-23','2026-10-24'):
            self.publish_day(day)

    def publish_day(self,day):
        url=f'https://example.com/{day}'
        self.service.enqueue('rss',{'url':url,'title':f'文章 {day}','published':time.time()})
        task=self.service.store.query('SELECT * FROM tasks WHERE url=?',(url,))[0]
        self.service.store.execute('UPDATE tasks SET day=? WHERE id=?',(day,task['id']))
        folder=self.settings.content_dir/str(task['id'])
        write_text(folder/'summary.json',json.dumps({'topic':'技术','bullets':['要点']},ensure_ascii=False))
        self.service.store.update(task['id'],status='done',summary_path=str(folder/'summary.json'))
        self.service.publish(day)

    def test_nested_details_render_with_year_and_month(self):
        page=self.client.get('/daily/2026-10-23').text
        self.assertIn('class="archive"',page)
        self.assertEqual(len(re.findall(r'class="archive-group"',page)),2)
        self.assertEqual(len(re.findall(r'class="archive-month"',page)),3)
        self.assertIn('2026 年',page)
        self.assertIn('10 月',page)
        self.assertIn('09 月',page)

    def test_only_current_month_is_expanded_in_markup(self):
        page=self.client.get('/daily/2026-10-23').text
        self.assertEqual(page.count('class="archive-month" open'),1)
        self.assertEqual(page.count('class="archive-group" open'),1)

    def test_active_day_is_marked_and_shows_day_number(self):
        page=self.client.get('/daily/2026-10-23').text
        self.assertIn('href="/daily/2026-10-23" title="2026-10-23" aria-label="2026-10-23" aria-current="page"',page)
        match=re.search(r'aria-label="2026-10-24">(\d+)<',page)
        self.assertEqual(match.group(1),'24','月分组下只显示日号')

    def test_flat_date_links_are_gone(self):
        page=self.client.get('/daily/2026-10-23').text
        self.assertNotIn('>2026-10-24</a>',page)
        self.assertNotIn('>2026-09-30</a>',page)

    def test_archive_day_count_still_reported(self):
        page=self.client.get('/daily/2026-10-23').text
        match=re.search(r'归档天数</dt><dd>(\d+) 天',page)
        self.assertEqual(match.group(1),'4')

    def test_empty_state_shows_placeholder(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(data_dir=Path(directory),session_secret='empty-archive-secret-32-chars-min',
                              admin_hash='offline-hash',secure_cookie=False)
            app=create_app(settings,run_background=False)
            with TestClient(app) as client:
                page=client.get('/').text
            self.assertIn('暂无归档',page)
            self.assertIn('class="archive"',page)

    def test_admin_page_not_affected(self):
        with TestClient(self.app) as client:
            client.cookies.set('daily_session',self.service.new_session(True))
            page=client.get('/admin').text
        # base.html 的共享样式表里必然有 .archive-* 选择器，这里只检查 HTML 标记。
        body=re.sub(r'<style>.*?</style>','',page,flags=re.S)
        self.assertNotIn('class="archive"',body)
        self.assertNotIn('class="archive-group"',body)
        self.assertNotIn('class="archive-days"',body)


if __name__=='__main__':unittest.main()
