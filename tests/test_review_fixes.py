"""发布链路与采集健壮性回归：覆盖评审发现的缺陷，不联网、不加载模型。"""
import io
import json
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from config import ROOT, Settings
from integration.bili_cookies import BiliSession
from integration.bili_refresh import cookie_value
from integration.cleanup import cleanup
from integration.files import write_text
from integration.sources import rss
from integration.sources.bili import BiliVideoSource, sanitize_cookie
from interaction.web import create_app
from orchestration.service import DailyService, day_of, normalize_day
from tests.test_bili_refresh import PassportAdapter


def settings_for(directory):
    return Settings(data_dir=Path(directory), session_secret='x' * 40,
                    admin_hash='offline', secure_cookie=False)


def add_done(service, url, bullets=None, day_stamp=None):
    stamp = time.time() if day_stamp is None else day_stamp
    service.enqueue('rss', {'url': url, 'title': url, 'published': stamp})
    task = service.store.query('SELECT * FROM tasks WHERE url=?', (url,))[0]
    path = service.settings.content_dir / str(task['id']) / 'summary.json'
    write_text(path, json.dumps({'topic': '技术', 'bullets': bullets or [url]}, ensure_ascii=False))
    service.store.update(task['id'], status='done', summary_path=str(path))
    return dict(service.store.task(task['id']), summary_path=str(path))


class PublishRobustnessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = settings_for(self.directory.name)
        self.service = DailyService(self.settings)

    def test_missing_summary_does_not_block_publish_or_tick(self):
        kept = add_done(self.service, 'https://example.com/a')
        broken = add_done(self.service, 'https://example.com/b')
        Path(broken['summary_path']).unlink()
        day = day_of(time.time())
        with patch.object(self.service, 'poll', return_value={'errors': [], 'capped': False}):
            self.service.tick()
        self.assertIsNotNone(self.service.store.latest(day))
        self.assertEqual(len(self.service.report(day)['entries']), 1)
        self.assertEqual(self.service.store.task(kept['id'])['status'], 'done')

    def test_one_broken_day_does_not_stop_other_days(self):
        old_stamp = time.time() - 3 * 86400
        old = add_done(self.service, 'https://example.com/old', day_stamp=old_stamp)
        broken = add_done(self.service, 'https://example.com/broken', day_stamp=old_stamp)
        today = add_done(self.service, 'https://example.com/today')
        Path(broken['summary_path']).unlink()
        with patch.object(self.service, 'poll', return_value={'errors': [], 'capped': False}):
            self.service.tick()
        self.assertIsNotNone(self.service.store.latest(old['day']))
        self.assertIsNotNone(self.service.store.latest(today['day']))

    def test_failed_task_keeps_previous_entry_and_recovers(self):
        add_done(self.service, 'https://example.com/a')
        b = add_done(self.service, 'https://example.com/b')
        day = day_of(time.time())
        self.service.publish(day)
        self.assertEqual(len(self.service.report(day)['entries']), 2)
        self.service.store.execute("UPDATE tasks SET status='failed' WHERE id=?", (b['id'],))
        self.service.publish(day)
        self.assertEqual(len(self.service.report(day)['entries']), 2)
        # 任务恢复后仍是2条，且活动版本与磁盘内容一致。
        self.service.store.execute("UPDATE tasks SET status='done' WHERE id=?", (b['id'],))
        self.service.publish(day)
        report = self.service.report(day)
        self.assertEqual(len(report['entries']), 2)
        latest = self.service.store.latest(day)
        self.assertEqual(json.loads(Path(latest['path']).read_text(encoding='utf-8'))['entries'], report['entries'])
        self.assertEqual(len(self.service.store.query('SELECT * FROM versions WHERE active=1')), 1)

    def test_reverting_content_reuses_version_without_orphan_files(self):
        add_done(self.service, 'https://example.com/a')
        day = day_of(time.time())
        self.service.publish(day)
        b = add_done(self.service, 'https://example.com/b')
        self.service.publish(day)
        self.service.store.execute('DELETE FROM tasks WHERE url=?', ('https://example.com/b',))
        for _ in range(5):
            self.service.publish(day)
        files = list((self.settings.data_dir / 'reports' / day).glob('*.json'))
        self.assertEqual(len(files), 2)
        self.assertEqual(len(self.service.store.query('SELECT * FROM versions WHERE active=1')), 1)
        self.assertEqual(self.service.store.latest(day)['id'], self.service.store.query('SELECT * FROM versions ORDER BY id')[0]['id'])
        self.assertIsNotNone(b)

    def test_idle_tick_skips_republish_when_signature_unchanged(self):
        add_done(self.service, 'https://example.com/a')
        day = day_of(time.time())
        with patch.object(self.service, 'poll', return_value={'errors': [], 'capped': False}):
            self.service.tick()
        first = self.service.store.latest(day)
        with patch.object(self.service, 'publish') as publish, \
                patch.object(self.service, 'poll', return_value={'errors': [], 'capped': False}):
            self.service.tick()
        publish.assert_not_called()
        self.assertEqual(self.service.store.latest(day)['id'], first['id'])

    def test_failed_publish_is_retried_on_next_tick(self):
        add_done(self.service, 'https://example.com/a')
        day = day_of(time.time())
        with patch.object(self.service, 'publish', side_effect=RuntimeError('private')), \
                patch.object(self.service, 'poll', return_value={'errors': [], 'capped': False}), \
                redirect_stdout(io.StringIO()):
            self.service.tick()
        self.assertIsNone(self.service.store.latest(day))
        with patch.object(self.service, 'poll', return_value={'errors': [], 'capped': False}):
            self.service.tick()
        self.assertIsNotNone(self.service.store.latest(day))

    def test_new_done_task_triggers_publish_on_next_tick(self):
        add_done(self.service, 'https://example.com/a')
        day = day_of(time.time())
        with patch.object(self.service, 'poll', return_value={'errors': [], 'capped': False}):
            self.service.tick()
        add_done(self.service, 'https://example.com/b')
        with patch.object(self.service, 'poll', return_value={'errors': [], 'capped': False}):
            self.service.tick()
        self.assertEqual(len(self.service.report(day)['entries']), 2)

    def test_cleanup_prunes_unreferenced_report_versions_only(self):
        add_done(self.service, 'https://example.com/a')
        day = day_of(time.time())
        self.service.publish(day)
        active = self.service.store.latest(day)
        orphan = self.settings.data_dir / 'reports' / day / 'orphan.json'
        write_text(orphan, '{}')
        result = cleanup(self.settings, self.service.store.query('SELECT id,status FROM tasks'))
        self.assertFalse(orphan.exists())
        self.assertTrue(Path(active['path']).exists())
        self.assertGreaterEqual(result['files'], 1)

    def test_cleanup_drops_expired_inactive_versions_with_their_rows(self):
        add_done(self.service, 'https://example.com/a')
        day = day_of(time.time())
        self.service.publish(day)
        old = self.service.store.latest(day)
        add_done(self.service, 'https://example.com/b')
        self.service.publish(day)
        current = self.service.store.latest(day)
        self.assertNotEqual(old['id'], current['id'])
        # 把非活动版本回拨到过期时间
        os.utime(old['path'], (time.time() - 30 * 86400,) * 2)
        cleanup(self.settings, self.service.store.query('SELECT id,status FROM tasks'))
        self.assertFalse(Path(old['path']).exists())
        self.assertIsNone(self.service.store.version_by_fingerprint(day, old['fingerprint'], old['final']))
        self.assertTrue(Path(current['path']).exists())
        self.assertEqual(self.service.store.latest(day)['id'], current['id'])

    def test_cleanup_never_removes_active_version(self):
        add_done(self.service, 'https://example.com/a')
        day = day_of(time.time())
        self.service.publish(day)
        active = self.service.store.latest(day)
        os.utime(active['path'], (time.time() - 400 * 86400,) * 2)
        cleanup(self.settings, self.service.store.query('SELECT id,status FROM tasks'))
        self.assertTrue(Path(active['path']).exists())

    def test_unreadable_active_version_does_not_crash_pages(self):
        add_done(self.service, 'https://example.com/a')
        day = day_of(time.time())
        self.service.publish(day)
        Path(self.service.store.latest(day)['path']).unlink()
        self.assertIsNone(self.service.report(day))
        with TestClient(create_app(self.settings, run_background=False)) as client:
            self.assertEqual(client.get('/').status_code, 200)

    def test_all_broken_summaries_do_not_publish_empty_report(self):
        task = add_done(self.service, 'https://example.com/a')
        day = day_of(time.time())
        Path(task['summary_path']).unlink()
        self.assertIsNone(self.service.publish(day))
        self.assertIsNone(self.service.store.latest(day))

    def test_missing_active_version_file_is_repaired_on_next_publish(self):
        add_done(self.service, 'https://example.com/a')
        day = day_of(time.time())
        self.service.publish(day)
        lost = self.service.store.latest(day)
        Path(lost['path']).unlink()
        self.assertIsNone(self.service.report(day))
        self.service.publish(day)
        repaired = self.service.store.latest(day)
        self.assertTrue(Path(repaired['path']).is_file())
        self.assertEqual(len(self.service.report(day)['entries']), 1)

    def test_rebuild_request_normalizes_day_and_reports_duplicate(self):
        self.assertEqual(normalize_day('2026-2-3'), '2026-02-03')
        self.assertTrue(self.service.request_rebuild('2026-2-3'))
        self.assertFalse(self.service.request_rebuild('2026-02-03'))
        self.assertEqual(self.service.dashboard()['rebuilds'][0]['day'], '2026-02-03')


class CookieRollbackTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'cookie.txt'
        with BiliSession(self.path) as session:
            session.import_login('SESSDATA=old-session; bili_jct=old-csrf; buvid3=device',
                                 'old-token', sanitize_cookie)
        self.source = BiliVideoSource(cookie_file=str(self.path), download_cache_dir=self.directory.name)
        self.addCleanup(self.source.session.close)
        self.adapter = PassportAdapter()
        self.source.session.mount('https://', self.adapter)

    def test_failed_save_after_rotation_keeps_new_token_not_stale_one(self):
        original_save = self.source.session._save
        failures = []

        def fail_first_save_after_rotation():
            # 模拟轮换成功后的瞬时写盘失败；重试可以成功。
            if self.source.session._metadata.get('pending_confirm') and not failures:
                failures.append(True)
                raise OSError('disk busy')
            original_save()

        with patch.object(self.source.session, '_save', side_effect=fail_first_save_after_rotation):
            with self.assertRaises(Exception):
                self.source._ensure_device_cookies()
        saved = BiliSession(self.path)
        self.addCleanup(saved.close)
        saved.load(None, sanitize_cookie)
        # 服务端已废止旧口令，文件中必须是新口令与待确认的旧口令，而不是新Cookie配旧口令。
        self.assertEqual(saved._metadata['refresh_token'], 'new-token')
        self.assertEqual(saved._metadata['pending_confirm'], 'old-token')
        self.assertEqual(cookie_value(saved, 'SESSDATA'), 'new-session')

    def test_permanent_save_failure_never_writes_new_cookie_with_old_token(self):
        original_save = self.source.session._save

        def always_fail_after_rotation():
            if self.source.session._metadata.get('pending_confirm'):
                raise OSError('disk full')
            original_save()

        with patch.object(self.source.session, '_save', side_effect=always_fail_after_rotation):
            with self.assertRaises(Exception):
                self.source._ensure_device_cookies()
        saved = BiliSession(self.path)
        self.addCleanup(saved.close)
        saved.load(None, sanitize_cookie)
        # 完全无法写盘时保留旧文件原样，绝不产生“新Cookie + 已作废旧口令”的组合。
        self.assertEqual(saved._metadata['refresh_token'], 'old-token')
        self.assertEqual(cookie_value(saved, 'SESSDATA'), 'old-session')
        self.assertEqual(self.source.session._metadata['refresh_token'], 'new-token')

    def test_rotation_failure_rolls_back_in_memory_cookies(self):
        self.adapter.rotate_error = True
        with self.assertRaises(Exception):
            self.source._ensure_device_cookies()
        self.assertEqual(cookie_value(self.source.session, 'SESSDATA'), 'old-session')
        saved = BiliSession(self.path)
        self.addCleanup(saved.close)
        saved.load(None, sanitize_cookie)
        self.assertEqual(cookie_value(saved, 'SESSDATA'), 'old-session')
        self.assertEqual(saved._metadata['refresh_token'], 'old-token')


class CollectorRobustnessTests(unittest.TestCase):
    def test_partial_download_is_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            page = Path(directory) / 'BV1xx411c7mD'
            page.mkdir()
            (page / 'P1.f137.mp4.part').write_bytes(b'partial')
            self.assertIsNone(BiliVideoSource._find_downloaded_file(str(page), 'P1'))
            (page / 'P1.m4a').write_bytes(b'audio')
            self.assertTrue(BiliVideoSource._find_downloaded_file(str(page), 'P1').endswith('P1.m4a'))

    def test_default_download_directory_exists_without_config(self):
        source = BiliVideoSource()
        self.addCleanup(source.session.close)
        self.assertTrue(source.download_cache_dir)

    def test_rss_undated_entry_uses_discovery_time(self):
        feed = b'''<rss version="2.0"><channel><title>News</title>
        <item><title>NoDate</title><link>https://example.com/undated</link><description>facts</description></item>
        </channel></rss>'''
        discovered = 1700000000.0
        with patch.object(rss, 'fetch', return_value=feed), \
                patch.object(rss, 'public_url', side_effect=lambda u: u):
            rows = rss.discover('https://example.com/rss', now=discovered)
        self.assertEqual(rows[0]['published'], discovered)

    def test_rss_skips_private_links_instead_of_failing_source(self):
        feed = b'''<rss version="2.0"><channel><title>News</title>
        <item><title>Private</title><link>http://127.0.0.1/secret</link></item>
        <item><title>Public</title><link>https://example.com/ok</link></item>
        </channel></rss>'''

        def check(url):
            if '127.0.0.1' in url:
                raise ValueError('不能访问私有网络地址')
            return url

        with patch.object(rss, 'fetch', return_value=feed), patch.object(rss, 'public_url', side_effect=check):
            rows = rss.discover('https://example.com/rss', now=1700000000.0)
        self.assertEqual([row['url'] for row in rows], ['https://example.com/ok'])

    def test_mid_extraction_rejects_unrelated_hosts(self):
        from integration.sources.bili import extract_mid_from_url
        self.assertEqual(extract_mid_from_url('123'), '123')
        self.assertEqual(extract_mid_from_url('https://space.bilibili.com/123?x=1'), '123')
        self.assertIsNone(extract_mid_from_url('https://example.com/abc/456'))
        self.assertIsNone(extract_mid_from_url('https://evil.test/1/2'))

    def test_poll_for_day_does_not_reset_source_timer(self):
        with tempfile.TemporaryDirectory() as directory:
            service = DailyService(settings_for(directory))
            service.add_source('bili', '123', 'test')
            last_poll = time.time() - 60
            service.store.execute('UPDATE sources SET last_poll=? WHERE id=1', (last_poll,))
            source = unittest.mock.Mock()
            source.fetch_page.return_value = ([], 0)
            with patch.object(service, 'bili', return_value=source):
                service.poll(day='2026-02-03')
            self.assertAlmostEqual(service.store.sources()[0]['last_poll'], last_poll, places=3)


class ConfigRobustnessTests(unittest.TestCase):
    def write(self, directory, text):
        path = Path(directory) / 'config.toml'
        path.write_text(text, encoding='utf-8')
        return path

    def template(self):
        return (ROOT / 'config.example.toml').read_text(encoding='utf-8')

    def test_missing_optional_sections_fall_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            text = self.template()
            for line in ('idle_seconds = 5', 'max_attempts = 3', 'chunk_chars = 12000',
                         'segment_seconds = 600', 'poll_seconds = 3600'):
                text = text.replace(line + '\n', '')
            path = self.write(directory, text)
            settings = Settings(config_file=path, data_dir=Path(directory) / 'data')
            self.assertEqual(settings.get('worker', 'idle_seconds', 5), 5)
            self.assertEqual(settings.get('llm', 'chunk_chars', 12000), 12000)
            self.assertEqual(settings.get('asr', 'segment_seconds', 600), 600)
            self.assertEqual(settings.get('pipeline', 'poll_seconds', 3600), 3600)

    def test_missing_site_urls_render_pages_instead_of_500(self):
        with tempfile.TemporaryDirectory() as directory:
            text = self.template().replace('public_url = "https://daily.example.com"\n', '')
            text = text.replace('nav_url = "https://nav.example.com"\n', '')
            path = self.write(directory, text)
            settings = Settings(config_file=path, data_dir=Path(directory) / 'data')
            self.assertEqual(settings.site_public_url, '')
            with TestClient(create_app(settings, run_background=False)) as client:
                response = client.get('/')
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('rel="canonical"', response.text)
                self.assertNotIn('返回导航', response.text)

    def test_invalid_llm_endpoint_rejected_at_startup(self):
        for value in ('http://example.com/v1', 'https://user:pass@example.com/v1', '""'):
            with tempfile.TemporaryDirectory() as directory:
                text = self.template().replace('base_url = "https://ark.cn-beijing.volces.com/api/plan/v3"',
                                               f'base_url = {value}')
                path = self.write(directory, text)
                with self.subTest(value=value), self.assertRaises(ValueError):
                    Settings(config_file=path, data_dir=Path(directory) / 'data')

    def test_missing_config_file_gives_first_run_hint(self):
        import io
        from contextlib import redirect_stdout
        from interaction.cli import main
        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'DAILY_CONFIG': str(Path(directory) / 'absent.toml')}, clear=False):
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main([]), 1)
        self.assertIn('config.example.toml', output.getvalue())

    def test_rebuild_duplicate_returns_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = settings_for(directory)
            app = create_app(settings, run_background=False)
            service = app.state.service
            session = service.new_session(True)
            with TestClient(app) as client:
                client.cookies.set('daily_session', session)
                token = service.session(session)['csrf']
                self.assertEqual(client.post('/admin/rebuild', data={'day': '2026-02-03', 'token': token},
                                             follow_redirects=False).status_code, 303)
                self.assertEqual(client.post('/admin/rebuild', data={'day': '2026-02-03', 'token': token},
                                             follow_redirects=False).status_code, 409)


class LockFileTests(unittest.TestCase):
    def test_lock_file_does_not_grow_per_acquisition(self):
        from integration.lock import worker_lock
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'x.lock'
            for _ in range(5):
                with worker_lock(str(path)):
                    pass
            self.assertEqual(path.stat().st_size, 1)


if __name__ == '__main__':
    unittest.main()
