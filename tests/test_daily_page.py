"""最新日报页：主题分组、预览行、翻页、元数据与 404 页面。"""
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
from orchestration.service import DailyService, day_of


class DailyPageTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings=Settings(data_dir=Path(self.directory.name),
                               session_secret='daily-page-secret-32-chars-minimum',
                               admin_hash='offline-hash',secure_cookie=False)
        self.app=create_app(self.settings,run_background=False)
        self.service=self.app.state.service
        self.client=TestClient(self.app)
        self.addCleanup(self.client.close)

    def add(self,day,topic,title,published=None,bullets=('要点一','要点二')):
        url=f'https://example.com/{day}/{title}'
        self.service.enqueue('rss',{'url':url,'title':title,'published':published or time.time()})
        task=self.service.store.query('SELECT * FROM tasks WHERE url=?',(url,))[0]
        self.service.store.execute('UPDATE tasks SET day=? WHERE id=?',(day,task['id']))
        folder=self.settings.content_dir/str(task['id'])
        payload={'bullets':list(bullets)}
        if topic is not None:
            payload['topic']=topic
        write_text(folder/'summary.json',json.dumps(payload,ensure_ascii=False))
        self.service.store.update(task['id'],status='done',summary_path=str(folder/'summary.json'))
        return task['id']

    def publish(self,day,specs):
        for index,(topic,title) in enumerate(specs):
            self.add(day,topic,title,time.time()-index*3600)
        self.service.publish(day)

    def sections(self,day):
        page=self.client.get(f'/daily/{day}').text
        return re.findall(r'<section class="topic" id="topic-([^"]+)">',page)

    def test_entry_without_topic_does_not_break_the_page(self):
        """模板里的 groupby 遇到 None 会抛 TypeError 让整页 500；这里锁定已修好的行为。"""
        self.publish('2026-10-24',[('技术','有主题'),(None,'没有主题的旧条目'),('技术','另一条技术')])
        response=self.client.get('/daily/2026-10-24')
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.sections('2026-10-24'),['技术','其他'])
        self.assertIn('没有主题的旧条目',response.text)

    def test_unknown_topic_folds_into_other(self):
        self.publish('2026-10-24',[('科技','未知主题一'),('技术','正常主题'),('科技','未知主题二')])
        self.assertEqual(self.sections('2026-10-24'),['技术','其他'])
        self.assertIn('未知主题二',self.client.get('/daily/2026-10-24').text)

    def test_sections_follow_editorial_order_not_insertion_order(self):
        specs=[('其他','a'),('行业','b'),('技术','c'),('研究','d'),('产品','e')]
        self.publish('2026-10-24',specs)
        report=self.service.report('2026-10-24')
        self.assertEqual([item['topic'] for item in DailyService.sections(report)],
                         ['技术','产品','行业','研究','其他'])

    def test_sections_are_ordered_by_vocabulary_even_when_input_is_reversed(self):
        specs=[('其他','o'),('研究','r'),('行业','i'),('产品','p'),('技术','t')]
        self.publish('2026-10-24',specs)
        report=self.service.report('2026-10-24')
        self.assertEqual([item['topic'] for item in DailyService.sections(report)],
                         ['技术','产品','行业','研究','其他'])

    def test_page_renders_sections_in_editorial_order(self):
        specs=[('其他','o1'),('研究','r1'),('行业','i1'),('产品','p1'),('技术','t1'),
               ('其他','o2'),('研究','r2'),('行业','i2'),('产品','p2'),('技术','t2')]
        self.publish('2026-10-24',specs)
        self.assertEqual(self.sections('2026-10-24'),['技术','产品','行业','研究','其他'])

    def test_preview_shows_first_bullet_and_hides_when_open(self):
        self.publish('2026-10-24',[('技术','标题一'),('技术','标题二')])
        page=self.client.get('/daily/2026-10-24').text
        self.assertIn('<p class="entry__preview">要点一</p>',page)
        self.assertIn('.entry[open] .entry__preview{display:none}',page)

    def test_entry_has_anchor_and_permalink(self):
        task_id=self.add('2026-10-24','技术','可分享的条目')
        self.service.publish('2026-10-24')
        page=self.client.get('/daily/2026-10-24').text
        self.assertIn(f'<details class="entry" id="entry-{task_id}">',page)
        self.assertIn(f'href="#entry-{task_id}"',page)

    def test_neighbors_follow_archive_order(self):
        for day in ('2026-10-22','2026-10-23','2026-10-24'):
            self.publish(day,[('技术',f'{day} 的条目')])
        self.assertEqual(self.service.neighbors('2026-10-23',self.service.dates()),
                         {'prev':'2026-10-22','next':'2026-10-24'})
        self.assertEqual(self.service.neighbors('2026-10-24',self.service.dates()),
                         {'prev':'2026-10-23','next':None})
        self.assertEqual(self.service.neighbors('2026-10-22',self.service.dates()),
                         {'prev':None,'next':'2026-10-23'})

    def test_pager_renders_links_and_boundary_text(self):
        for day in ('2026-10-23','2026-10-24'):
            self.publish(day,[('技术',f'{day} 的条目')])
        middle=self.client.get('/daily/2026-10-23').text
        self.assertIn('href="/daily/2026-10-24" rel="next"',middle)
        self.assertIn('这是第一份日报',middle)
        latest=self.client.get('/daily/2026-10-24').text
        self.assertIn('href="/daily/2026-10-23" rel="prev"',latest)
        self.assertIn('已是最新一期',latest)

    def test_older_day_offers_return_to_latest(self):
        for day in ('2026-10-23','2026-10-24'):
            self.publish(day,[('技术',f'{day} 的条目')])
        older=self.client.get('/daily/2026-10-23').text
        self.assertIn('回到最新 2026-10-24',older)
        latest=self.client.get('/daily/2026-10-24').text
        self.assertNotIn('回到最新',latest)

    def test_stats_and_aside_are_filled(self):
        self.publish('2026-10-24',[('技术','a'),('技术','b'),('行业','c')])
        page=self.client.get('/daily/2026-10-24').text
        self.assertIn('<dt>收录条数</dt><dd>3 条</dd>',page)
        self.assertIn('<dt>来源数</dt><dd>1 个</dd>',page)
        self.assertIn('<dt>涉及主题</dt><dd>2 个</dd>',page)
        self.assertIn('<dt>归档天数</dt><dd>1 天</dd>',page)
        self.assertIn('<dt>内容时段</dt><dd>',page)

    def test_title_and_description_carry_the_day(self):
        self.publish('2026-10-24',[('技术','标题一'),('行业','标题二')])
        page=self.client.get('/daily/2026-10-24').text
        self.assertIn('<title>2026-10-24 · 鸦闻 · 每日简报</title>',page)
        self.assertIn('<meta name="description" content="2026-10-24 收录 2 条：',page)
        self.assertIn('<meta property="og:title" content="2026-10-24 · 鸦闻 · 每日简报">',page)
        self.assertIn('<meta property="og:type" content="article">',page)

    def test_home_page_is_not_left_without_a_date_title(self):
        self.publish('2026-10-24',[('技术','标题')])
        page=self.client.get('/').text
        self.assertIn('<title>2026-10-24 · 鸦闻 · 每日简报</title>',page)
        self.assertNotIn('回到最新',page)

    def test_topic_nav_links_match_section_ids(self):
        self.publish('2026-10-24',[('技术','a'),('技术','b'),('行业','c')])
        page=self.client.get('/daily/2026-10-24').text
        for topic in ('技术','行业'):
            self.assertIn(f'<a class="chip" href="#topic-{topic}">',page)
            self.assertIn(f'<section class="topic" id="topic-{topic}">',page)

    def test_single_topic_skips_the_nav(self):
        self.publish('2026-10-24',[('技术','a'),('技术','b')])
        page=self.client.get('/daily/2026-10-24').text
        self.assertNotIn('<nav class="topic-nav" aria-label="主题导航">',page)

    def test_missing_day_renders_html_instead_of_json(self):
        self.publish('2026-10-24',[('技术','最新')])
        response=self.client.get('/daily/2026-01-01')
        self.assertEqual(response.status_code,404)
        self.assertIn('text/html',response.headers['content-type'])
        self.assertIn('这一期不在架上',response.text)
        self.assertIn('回到最新一期（2026-10-24）',response.text)

    def test_malformed_day_is_rejected_with_the_same_page(self):
        response=self.client.get('/daily/2026-1-1')
        self.assertEqual(response.status_code,404)
        self.assertIn('text/html',response.headers['content-type'])

    def test_fully_retracted_day_is_a_404_page(self):
        task_id=self.add('2026-10-24','技术','唯一一条')
        self.service.publish('2026-10-24')
        self.service.retract_entry(task_id,'不实信息')
        page=self.client.get('/daily/2026-10-24')
        self.assertEqual(page.status_code,404)
        self.assertIn('这一期不在架上',page.text)

    def test_today_without_report_is_flagged(self):
        yesterday=day_of(time.time()-86400)
        self.publish(yesterday,[('技术','昨天')])
        page=self.client.get('/daily/'+yesterday).text
        self.assertIn('今日尚未更新，以下为最近一期',page)
        self.assertIn('class="chip chip--muted"',page)

    def test_no_stale_notice_when_today_is_published(self):
        today=day_of(time.time())
        self.publish(today,[('技术','今天')])
        page=self.client.get('/daily/'+today).text
        self.assertNotIn('今日尚未更新',page)

    def test_stale_notice_links_to_admin_only_when_authenticated(self):
        yesterday=day_of(time.time()-86400)
        self.publish(yesterday,[('技术','昨天')])
        anonymous=self.client.get('/daily/'+yesterday).text
        self.assertNotIn('去管理页',anonymous)
        with TestClient(self.app) as client:
            client.cookies.set('daily_session',self.service.new_session(True))
            page=client.get('/daily/'+yesterday).text
        self.assertIn('href="/admin">去管理页',page)

    def test_empty_state_offers_admin_entry_only_when_authenticated(self):
        anonymous=self.client.get('/').text
        self.assertIn('尚未发布第一份日报',anonymous)
        self.assertNotIn('去管理页添加来源',anonymous)
        with TestClient(self.app) as client:
            client.cookies.set('daily_session',self.service.new_session(True))
            page=client.get('/').text
        self.assertIn('去管理页添加来源',page)

    def test_print_and_mobile_order_are_declared(self):
        self.publish('2026-10-24',[('技术','a')])
        page=self.client.get('/daily/2026-10-24').text
        self.assertIn('@media print{',page)
        self.assertIn('grid-template-areas:"main" "nav" "info"',page)
        # 正文在 DOM 中先于归档，窄屏才能先读到当天内容。
        self.assertLess(page.index('class="layout__main"'),page.index('class="nav-list layout__nav"'))

    def test_public_contracts_are_unchanged(self):
        self.publish('2026-10-24',[('技术','标题')])
        self.assertEqual(self.client.get('/api/public/days').json(),['2026-10-24'])
        payload=self.client.get('/api/public/daily/2026-10-24').json()
        self.assertEqual(set(payload),{'day','updated','final','supplement','entries'})
        self.assertEqual(payload['entries'][0]['title'],'标题')

    def test_sections_do_not_mutate_the_report(self):
        report={'day':'2026-10-24','entries':[{'id':1,'topic':'技术'},{'id':2}]}
        self.assertEqual([s['topic'] for s in DailyService.sections(report)],['技术','其他'])
        self.assertNotIn('topic',report['entries'][1])


if __name__=='__main__':unittest.main()
