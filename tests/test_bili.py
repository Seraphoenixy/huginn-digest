import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from config import Settings
from integration.sources.bili import BiliVideoSource, BiliSourceError
from orchestration.service import DailyService


class BiliTests(unittest.TestCase):
    def source(self):
        source=BiliVideoSource(download_cache_dir='unused')
        source._device_cookies_ready=True
        source._sign_wbi_params=Mock(side_effect=lambda p:p)
        return source

    def test_risk_error_is_not_empty_success(self):
        source=self.source()
        response=Mock(status_code=200)
        response.json.return_value={'code':-352,'message':'private-response'}
        source.session.get=Mock(return_value=response)
        source._login_status=False
        with self.assertRaises(BiliSourceError) as raised:source.fetch_page('123',1,30)
        self.assertIn('未登录',str(raised.exception))
        self.assertNotIn('private-response',str(raised.exception))
        source.session.close()

    def test_http_and_network_failures_raise(self):
        for status in (412,429):
            source=self.source()
            source.session.get=Mock(return_value=Mock(status_code=status))
            with self.assertRaises(BiliSourceError):source.fetch_page('123',1,30)
            source.session.close()
        source=self.source()
        source.session.get=Mock(side_effect=RuntimeError('private-error'))
        with self.assertRaises(BiliSourceError) as raised:source.fetch_page('123',1,30)
        self.assertNotIn('private-error',str(raised.exception))
        source.session.close()

    def test_legitimate_empty_list_remains_success(self):
        source=self.source();response=Mock(status_code=200)
        response.json.return_value={'code':0,'data':{'page':{'count':0},'list':{'vlist':[]}}}
        source.session.get=Mock(return_value=response)
        self.assertEqual(source.fetch_page('123',1,30),([],0))
        source.session.close()

    def test_poll_records_error_and_manual_refresh_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            service=DailyService(Settings(data_dir=Path(directory)))
            service.add_source('bili','123','test')
            source=Mock()
            source.fetch_page.side_effect=BiliSourceError(-352,False)
            with patch.object(service,'bili',return_value=source):service.poll()
            self.assertIn('-352',service.store.sources()[0]['error'])
            self.assertEqual(service.dashboard()['tasks'],[])
            source.fetch_page.side_effect=None
            source.fetch_page.return_value=([],0)
            service.source_action(1,'refresh')
            with patch.object(service,'bili',return_value=source):service.poll()
            self.assertEqual(service.store.sources()[0]['error'],'')

    def test_download_cookies_scoped_and_no_deprecated_header(self):
        from yt_dlp.cookies import YoutubeDLCookieJar
        with tempfile.TemporaryDirectory() as directory:
            source=BiliVideoSource(cookie='SESSDATA=test-session; bili_jct=test-csrf',download_cache_dir=directory)
            source._device_cookies_ready=True
            source.session.cookies.set('buvid3','test-device',domain='.bilibili.com')
            source.session.cookies.set('unrelated','test-other',domain='.example.com')
            source.session.cookies.set('hostless','test-hostless')
            jar=YoutubeDLCookieJar()
            downloader=Mock()
            downloader.cookiejar=jar
            downloader.__enter__=Mock(return_value=downloader)
            downloader.__exit__=Mock(return_value=False)
            def download(urls):
                path=Path(directory)/'BV1Mbp76oESC'/'P1.m4a'
                path.write_bytes(b'audio-fixture')
            downloader.download.side_effect=download
            with patch('yt_dlp.YoutubeDL',return_value=downloader) as factory:
                self.assertTrue(source.download_media('BV1Mbp76oESC').endswith('P1.m4a'))
            self.assertNotIn('Cookie',factory.call_args.args[0]['http_headers'])
            for url in ['https://www.bilibili.com/video/test','https://api.bilibili.com/x/test']:
                header=jar.get_cookie_header(url)
                self.assertIn('SESSDATA=',header)
                self.assertIn('buvid3=',header)
                self.assertNotIn('unrelated=',header)
                self.assertNotIn('hostless=',header)
            for url in ['https://example.com/','https://cdn.bilivideo.com/','https://bilibili.com.example.org/','http://api.bilibili.com/']:
                self.assertFalse(jar.get_cookie_header(url))
            source.session.close()

    def test_empty_download_cookie_jar_supported(self):
        from yt_dlp.cookies import YoutubeDLCookieJar
        source=self.source()
        jar=YoutubeDLCookieJar()
        source._populate_download_cookies(jar)
        self.assertEqual(len(list(jar)),0)
        source.session.close()


if __name__=='__main__':unittest.main()
